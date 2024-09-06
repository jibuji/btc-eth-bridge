from fastapi import FastAPI, HTTPException, BackgroundTasks
from web3 import Web3
from pydantic import BaseModel
import os
from dotenv import load_dotenv
from bitcoinrpc.authproxy import AuthServiceProxy, JSONRPCException
from decimal import Decimal
from enum import Enum
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, func, Enum as SQLAlchemyEnum
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import declarative_base
from datetime import datetime, timedelta
from web3.exceptions import TimeExhausted, TransactionNotFound
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import atexit
import time
from sqlalchemy.exc import IntegrityError
import traceback
import logging

load_dotenv()

app = FastAPI()

# Check for required environment variables
required_env_vars = [
    'ETH_NODE_URL', 'BTC_NODE_URL', 'DATABASE_URL', 'OWNER_ADDRESS',
    'OWNER_PRIVATE_KEY', 'WBTC_ADDRESS', 'BTC_WALLET_NAME', 'BRIDGE_BTC_ADDRESS'
]

missing_vars = {var: os.getenv(var) for var in required_env_vars if not os.getenv(var)}

if missing_vars:
    raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars.keys())}")

# Database setup
engine = create_engine(os.getenv('DATABASE_URL'))
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class TransactionStatus(Enum):
    PENDING = "pending"
    MINTING = "minting"
    MINTING_FAILED = "minting_failed"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    PENDING_CONFIRMATIONS = "pending_confirmations"

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    btc_tx_id = Column(String, unique=True, index=True)
    eth_address = Column(String)
    amount = Column(Float)
    status = Column(SQLAlchemyEnum(TransactionStatus), default=TransactionStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    eth_tx_hash = Column(String)

Base.metadata.create_all(bind=engine)

# Web3 and Bitcoin RPC setup
w3 = Web3(Web3.HTTPProvider(os.getenv('ETH_NODE_URL')))
btc_rpc = None
BTC_WALLET_NAME = os.getenv('BTC_WALLET_NAME')

def load_btc_wallet():
    global btc_rpc
    try:
        base_url = os.getenv('BTC_NODE_URL')
        btc_rpc = AuthServiceProxy(base_url)
        
        wallet_info = btc_rpc.listwalletdir()
        wallet_exists = any(wallet['name'] == BTC_WALLET_NAME for wallet in wallet_info['wallets'])
        
        if not wallet_exists:
            btc_rpc.createwallet(BTC_WALLET_NAME)
        else:
            try:
                btc_rpc.loadwallet(BTC_WALLET_NAME)
            except JSONRPCException as e:
                if "already loaded" not in str(e):
                    raise
        
        wallet_url = f"{base_url}/wallet/{BTC_WALLET_NAME}"
        btc_rpc = AuthServiceProxy(wallet_url)
        btc_rpc.getwalletinfo()  # Test the connection
        
    except JSONRPCException as e:
        print(f"Error loading wallet: {str(e)}")
        raise

# Load the wallet before starting the server
load_btc_wallet()

# WBTC contract setup
WBTC_ADDRESS = os.getenv('WBTC_ADDRESS')
WBTC_ABI = [
    {"inputs": [{"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}], "name": "mint", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "from", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}], "name": "burn", "outputs": [], "stateMutability": "nonpayable", "type": "function"}
]
wbtc_contract = w3.eth.contract(address=WBTC_ADDRESS, abi=WBTC_ABI)

class WrapRequest(BaseModel):
    btc_tx_id: str
    ethereum_address: str

class UnwrapRequest(BaseModel):
    ethereum_address: str
    bitcoin_address: str
    amount: float

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def verify_btc_transaction(tx_id: str, required_confirmations: int = 3):
    max_retries = 3
    retry_delay = 2  # seconds

    for attempt in range(max_retries):
        try:
            logger.info(f"Verifying BTC transaction {tx_id} (attempt {attempt + 1})")
            tx = btc_rpc.gettransaction(tx_id, True)
            confirmations = tx.get('confirmations', 0)
            if confirmations < required_confirmations:
                raise ValueError(f"Transaction needs {required_confirmations} confirmations, but has only {confirmations}")
            
            bridge_address = os.getenv('BRIDGE_BTC_ADDRESS')
            
            if 'vout' not in tx:
                logger.debug("Transaction does not contain 'vout' data. Checking 'details' instead.")
                for detail in tx.get('details', []):
                    if detail.get('address') == bridge_address and detail.get('category') == 'receive':
                        return detail['amount']
            else:
                for vout in tx['vout']:
                    if 'scriptPubKey' in vout and 'addresses' in vout['scriptPubKey']:
                        if bridge_address in vout['scriptPubKey']['addresses']:
                            return vout['value']
            
            raise ValueError("Transaction does not have output to bridge's BTC address")
        except JSONRPCException as e:
            logger.warning(f"RPC error on attempt {attempt + 1}: {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                raise ValueError(f"Failed to verify BTC transaction after {max_retries} attempts: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error on attempt {attempt + 1}: {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                raise ValueError(f"Failed to verify BTC transaction: {str(e)}")

    raise ValueError("Failed to verify BTC transaction after all attempts")

def create_transaction(db, btc_tx_id, eth_address, amount):
    transaction = Transaction(
        btc_tx_id=btc_tx_id,
        eth_address=eth_address,
        amount=amount,
        status=TransactionStatus.PENDING
    )
    db.add(transaction)
    return transaction

def update_transaction_status(db, transaction, status):
    transaction.status = status
    transaction.updated_at = datetime.utcnow()
    db.commit()

@app.post("/initiate-wrap")
async def initiate_wrap(request: WrapRequest, background_tasks: BackgroundTasks):
    db = SessionLocal()
    try:
        # Check if transaction already exists
        existing_transaction = db.query(Transaction).filter_by(btc_tx_id=request.btc_tx_id).first()
        if existing_transaction:
            if existing_transaction.status == TransactionStatus.COMPLETED:
                raise HTTPException(status_code=400, detail="Transaction already processed")
            elif existing_transaction.status in [TransactionStatus.PENDING, TransactionStatus.PENDING_CONFIRMATIONS]:
                return {"message": "Wrap already in progress"}
            elif existing_transaction.status == TransactionStatus.MINTING_FAILED:
                # We might want to allow retrying failed transactions
                background_tasks.add_task(mint_wbtc, existing_transaction.id, existing_transaction.eth_address, int(existing_transaction.amount * 10**8))
                return {"message": "Previous minting failed. Retrying minting process."}
            elif existing_transaction.status == TransactionStatus.FAILED:
                # This is for other types of failures
                raise HTTPException(status_code=400, detail="Transaction previously failed. Please contact support.")
            elif existing_transaction.status == TransactionStatus.EXPIRED:
                raise HTTPException(status_code=400, detail="Transaction expired. Please initiate a new transaction.")
            else:
                # This catches any unexpected status values
                raise HTTPException(status_code=500, detail=f"Transaction in unexpected state: {existing_transaction.status}")

        # Verify the Bitcoin transaction
        btc_amount = verify_btc_transaction(request.btc_tx_id)
        
        # Create new transaction record
        try:
            transaction = create_transaction(db, request.btc_tx_id, request.ethereum_address, btc_amount)
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"message": "Wrap already in progress..."}
        
        # Convert BTC amount to Wei
        amount_wei = int(btc_amount * 10**8)
        
        # Add minting task to background tasks
        background_tasks.add_task(mint_wbtc, transaction.id, request.ethereum_address, amount_wei)
        
        return {"message": "Wrap initiated, WBTC will be minted after confirmation"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {str(e)}")
    finally:
        db.close()

def mint_wbtc(transaction_id: int, ethereum_address: str, amount_wei: int):
    db = SessionLocal()
    try:
        transaction = db.get(Transaction, transaction_id)
        if not transaction or transaction.status not in [TransactionStatus.PENDING, TransactionStatus.MINTING_FAILED]:
            logger.warning(f"Transaction {transaction_id} not found or not in PENDING or MINTING_FAILED state")
            return
        
        update_transaction_status(db, transaction, TransactionStatus.MINTING)
        
        owner_address = os.getenv('OWNER_ADDRESS')
        nonce = w3.eth.get_transaction_count(owner_address)
        logger.info(f"Current nonce for {owner_address}: {nonce}")
        
        tx = wbtc_contract.functions.mint(
            ethereum_address,
            amount_wei
        ).build_transaction({
            'from': owner_address,
            'nonce': nonce,
            'gas': 2000000,  # Adjust this value as needed
            'gasPrice': w3.eth.gas_price,
        })
        
        logger.debug(f"Transaction built: {tx}")
        
        signed_tx = w3.eth.account.sign_transaction(tx, os.getenv('OWNER_PRIVATE_KEY'))
        logger.info("Transaction signed")
        
        try:
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            logger.info(f"Transaction sent. Hash: {tx_hash.hex()}")
            
            # Update transaction with eth_tx_hash
            transaction.eth_tx_hash = tx_hash.hex()
            db.commit()
            
            # Wait for transaction receipt with a timeout
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)  # Reduced timeout for testing
            logger.info(f"Transaction mined. Receipt: {receipt}")
            
            # Check for sufficient confirmations
            current_block = w3.eth.block_number
            confirmations = current_block - receipt['blockNumber'] + 1
            
            if confirmations >= 1:  # Reduced for testing on local node
                update_transaction_status(db, transaction, TransactionStatus.COMPLETED)
                logger.info(f"WBTC minted successfully. Transaction hash: {tx_hash.hex()}")
            else:
                update_transaction_status(db, transaction, TransactionStatus.PENDING_CONFIRMATIONS)
                logger.info(f"WBTC minted, waiting for more confirmations. Current: {confirmations}. Transaction: {tx_hash.hex()}")
        except Exception as e:
            error_trace = traceback.format_exc()
            update_transaction_status(db, transaction, TransactionStatus.MINTING_FAILED)
            logger.error(f"Failed to mint WBTC: {str(e)}")
            logger.error(f"Error trace: {error_trace}")
    except Exception as e:
        error_trace = traceback.format_exc()
        update_transaction_status(db, transaction, TransactionStatus.MINTING_FAILED)
        logger.error(f"Failed to mint WBTC: {str(e)}")
        logger.error(f"Error trace: {error_trace}")
    finally:
        db.close()

# Helper function to check transaction status
def check_transaction_status(tx_hash):
    try:
        tx = w3.eth.get_transaction(tx_hash)
        if tx is None:
            return "Transaction not found"
        if tx['blockNumber'] is None:
            return "Pending"
        return f"Mined in block {tx['blockNumber']}"
    except TransactionNotFound:
        return "Transaction not found"

# You can call this function after minting to check the status
# print(check_transaction_status(tx_hash.hex()))

def check_pending_confirmations():
    db = SessionLocal()
    try:
        pending_txs = db.query(Transaction).filter_by(status=TransactionStatus.PENDING_CONFIRMATIONS).all()
        logger.info("Pending transactions:")
        for tx in pending_txs:
            logger.info(f"ID: {tx.id}")
            logger.info(f"BTC Transaction ID: {tx.btc_tx_id}")
            logger.info(f"Ethereum Address: {tx.eth_address}")
            logger.info(f"Amount: {tx.amount}")
            logger.info(f"Status: {tx.status}")
            logger.info(f"Created At: {tx.created_at}")
            logger.info(f"Updated At: {tx.updated_at}")
            try:
                receipt = w3.eth.get_transaction_receipt(tx.eth_tx_hash)
                if receipt:
                    current_block = w3.eth.block_number
                    confirmations = current_block - receipt['blockNumber'] + 1
                    if confirmations >= 6:
                        update_transaction_status(db, tx, TransactionStatus.COMPLETED)
                        logger.info(f"Transaction {tx.eth_tx_hash} now has {confirmations} confirmations. Marked as completed.")
                else:
                    logger.info(f"Transaction {tx.eth_tx_hash} is still pending.")
            except TransactionNotFound:
                logger.warning(f"Transaction {tx.eth_tx_hash} not found. It may have been dropped or not broadcast.")
                time_since_creation = datetime.utcnow() - tx.created_at
                if time_since_creation > timedelta(hours=24):
                    update_transaction_status(db, tx, TransactionStatus.FAILED)
                    logger.warning(f"Transaction {tx.eth_tx_hash} marked as failed due to being unfound for over 24 hours.")
    except Exception as e:
        logger.error(f"Error in check_pending_confirmations: {str(e)}")
    finally:
        db.close()

def retry_failed_mints():
    db = SessionLocal()
    try:
        failed_transactions = db.query(Transaction).filter_by(status=TransactionStatus.MINTING_FAILED).all()
        for tx in failed_transactions:
            logger.info(f"Retrying failed mint for transaction {tx.id}")
            mint_wbtc(tx.id, tx.eth_address, int(tx.amount * 10**8))
    finally:
        db.close()

def reconcile_balances():
    db = SessionLocal()
    try:
        total_btc_received = db.query(func.sum(Transaction.amount)).filter_by(status=TransactionStatus.COMPLETED).scalar() or 0
        total_wbtc_minted = wbtc_contract.functions.totalSupply().call() / 10**8
        
        if abs(total_btc_received - total_wbtc_minted) > 0.00001:  # Allow for small discrepancies due to rounding
            logger.warning(f"Balance mismatch detected: BTC received: {total_btc_received}, WBTC minted: {total_wbtc_minted}")
            # Send an alert to the system administrators
    finally:
        db.close()

# The check_new_block function will run with each new Ethereum block:
def check_new_block():
    current_block = w3.eth.block_number
    logger.info(f"Current block: {current_block}")
    if current_block > check_new_block.last_checked_block:
        check_pending_confirmations()
        check_new_block.last_checked_block = current_block

check_new_block.last_checked_block = w3.eth.block_number


scheduler = BackgroundScheduler()

# Define job configurations
job_configs = [
    {
        'func': retry_failed_mints,
        'trigger': IntervalTrigger(minutes=30),
        'id': 'retry_failed_mints_job',
        'name': 'Retry failed WBTC mints'
    },
    {
        'func': reconcile_balances,
        'trigger': IntervalTrigger(hours=1),
        'id': 'reconcile_balances_job',
        'name': 'Reconcile BTC and WBTC balances'
    },
    {
        'func': check_new_block,
        'trigger': IntervalTrigger(seconds=15),
        'id': 'check_new_block_job',
        'name': 'Check for new Ethereum blocks'
    }
]

# Add jobs to the scheduler
for job in job_configs:
    scheduler.add_job(
        func=job['func'],
        trigger=job['trigger'],
        id=job['id'],
        name=job['name'],
        replace_existing=True
    )

# Start the scheduler
scheduler.start()

# Make sure to shut down the scheduler when your app is shutting down
atexit.register(lambda: scheduler.shutdown())


@app.post("/initiate-unwrap")
async def initiate_unwrap(request: UnwrapRequest, background_tasks: BackgroundTasks):
    db = SessionLocal()
    try:
        # Convert BTC amount to Wei
        amount_wei = int(request.amount * 10**8)
        
        # Check if the bridge has enough BTC balance
        bridge_balance = float(btc_rpc.getbalance())
        if bridge_balance < request.amount:
            raise HTTPException(status_code=400, detail="Insufficient BTC balance in the bridge")
        
        # Create new transaction record
        transaction = create_transaction(db, f"unwrap_{datetime.utcnow().timestamp()}", request.ethereum_address, request.amount)
        
        # Add unwrap task to background tasks
        background_tasks.add_task(process_unwrap, transaction.id, request.ethereum_address, request.bitcoin_address, amount_wei)
        
        return {"message": "Unwrap initiated"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()

def process_unwrap(transaction_id: int, ethereum_address: str, bitcoin_address: str, amount_wei: int):
    db = SessionLocal()
    try:
        transaction = db.query(Transaction).get(transaction_id)
        if not transaction or transaction.status != TransactionStatus.PENDING:
            return
        
        # Burn WBTC
        burn_tx_hash = burn_wbtc(ethereum_address, amount_wei)
        
        # Initiate BTC transfer
        btc_amount = amount_wei / 10**8
        btc_tx = btc_rpc.sendtoaddress(bitcoin_address, btc_amount)
        
        update_transaction_status(db, transaction, TransactionStatus.COMPLETED)
        logger.info(f"Unwrap completed. ETH burn tx: {burn_tx_hash.hex()}, BTC tx: {btc_tx}")
    except Exception as e:
        update_transaction_status(db, transaction, TransactionStatus.FAILED)
        logger.error(f"Failed to process unwrap: {str(e)}")
    finally:
        db.close()

def burn_wbtc(ethereum_address: str, amount_wei: int):
    tx = wbtc_contract.functions.burn(
        ethereum_address,
        amount_wei
    ).build_transaction({
        'from': os.getenv('OWNER_ADDRESS'),
        'nonce': w3.eth.get_transaction_count(os.getenv('OWNER_ADDRESS')),
    })
    
    signed_tx = w3.eth.account.sign_transaction(tx, os.getenv('OWNER_PRIVATE_KEY'))
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    
    # Wait for transaction confirmation
    w3.eth.wait_for_transaction_receipt(tx_hash)
    
    return tx_hash

# Periodic tasks
def expire_old_transactions():
    db = SessionLocal()
    try:
        expiration_time = datetime.utcnow() - timedelta(hours=24)
        expired_transactions = db.query(Transaction).filter(
            Transaction.status == TransactionStatus.PENDING,
            Transaction.created_at < expiration_time
        ).all()
        for transaction in expired_transactions:
            update_transaction_status(db, transaction, TransactionStatus.EXPIRED)
        logger.info(f"Expired {len(expired_transactions)} transactions")
    finally:
        db.close()

@app.post("/retry-mint/{transaction_id}")
async def retry_mint(transaction_id: int):
    db = SessionLocal()
    try:
        transaction = db.get(Transaction, transaction_id)
        if not transaction:
            raise HTTPException(status_code=404, detail="Transaction not found")
        if transaction.status != TransactionStatus.MINTING_FAILED:
            raise HTTPException(status_code=400, detail="Transaction is not in a minting failed state")
        
        mint_wbtc(transaction.id, transaction.eth_address, int(transaction.amount * 10**8))
        return {"message": "Mint retry initiated"}
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)