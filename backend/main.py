from fastapi import FastAPI, HTTPException, BackgroundTasks
from web3 import Web3
from pydantic import BaseModel
import os
from dotenv import load_dotenv
from bitcoin import rpc
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
from typing import Optional
from urllib.parse import urlparse
import http.client
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

load_dotenv()

app = FastAPI()

# Database setup
engine = create_engine(os.getenv('DATABASE_URL'))
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class TransactionStatus(Enum):
    RECEIVED = "received"
    BROADCASTED = "broadcasted"
    CONFIRMING = "confirming"
    MINTING = "minting"
    COMPLETED = "completed"
    FAILED = "failed"

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    btc_tx_id = Column(String, unique=True, index=True)
    eth_address = Column(String)
    amount = Column(Float, nullable=True)
    status = Column(SQLAlchemyEnum(TransactionStatus), default=TransactionStatus.RECEIVED)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    eth_tx_hash = Column(String, nullable=True)

Base.metadata.create_all(bind=engine)

# Web3 and Bitcoin RPC setup
w3 = Web3(Web3.HTTPProvider(os.getenv('ETH_NODE_URL')))

btc_rpc = None
BTC_WALLET_NAME = os.getenv('BTC_WALLET_NAME')  

def load_btc_wallet():
    new_btc_rpc = None
    try:
        # Parse BTC_NODE_URL
        btc_node_url = os.getenv('BTC_NODE_URL')
        if not btc_node_url:
            raise ValueError("BTC_NODE_URL not set in .env file")

        parsed_url = urlparse(btc_node_url)
        
        # Ensure the URL contains authentication info
        if not parsed_url.username or not parsed_url.password:
            raise ValueError("BTC_NODE_URL must include username and password")

        # Create RPC connection
        new_btc_rpc = rpc.RawProxy(service_url=btc_node_url, timeout=120)
        
        wallet_info = new_btc_rpc.listwalletdir()
        wallet_exists = any(wallet['name'] == BTC_WALLET_NAME for wallet in wallet_info['wallets'])
        
        if not wallet_exists:
            new_btc_rpc.createwallet(BTC_WALLET_NAME)
        else:
            try:
                new_btc_rpc.loadwallet(BTC_WALLET_NAME)
            except rpc.JSONRPCError as e:
                if "already loaded" not in str(e):
                    raise

        wallet_url = f"{btc_node_url}/wallet/{BTC_WALLET_NAME}"
        print("wallet_url:", wallet_url)
        new_btc_rpc = rpc.RawProxy(service_url=wallet_url, timeout=1200)
        walletInfo = new_btc_rpc.getwalletinfo()  # Test the connection
        # print("walletInfo:", walletInfo)

        # txInfo = new_btc_rpc.gettransaction("a2410f9bda7a4ebdec2b873c5c9db5f20e4f78f7ed93f12006aa28f449168eaa")
        # print("txInfo:", txInfo)
        return new_btc_rpc

    except rpc.JSONRPCError as e:
        print(f"Error loading wallet: {str(e)}")
        raise
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        raise

# Load the wallet before starting the server
btc_rpc = load_btc_wallet()

# WBTC contract setup
WBTC_ADDRESS = os.getenv('WBTC_ADDRESS')
WBTC_ABI = [
    {"inputs": [{"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}], "name": "mint", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "from", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}], "name": "burn", "outputs": [], "stateMutability": "nonpayable", "type": "function"}
]
wbtc_contract = w3.eth.contract(address=WBTC_ADDRESS, abi=WBTC_ABI)

class WrapRequest(BaseModel):
    signed_btc_tx: str
    ethereum_address: str

class WrapStatus(BaseModel):
    btc_tx_id: str
    status: TransactionStatus
    amount: Optional[float]
    ethereum_address: str
    created_at: datetime
    updated_at: datetime
    eth_tx_hash: Optional[str] = None

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def create_transaction(db, btc_tx_id, eth_address, amount):
    transaction = Transaction(
        btc_tx_id=btc_tx_id,
        eth_address=eth_address,
        amount=amount,
        status=TransactionStatus.RECEIVED
    )
    db.add(transaction)
    return transaction

def update_transaction_status(db, transaction, status):
    transaction.status = status
    transaction.updated_at = datetime.utcnow()
    db.commit()

@app.post("/initiate-wrap")
async def initiate_wrap(request: WrapRequest):
    db = SessionLocal()
    try:
        # Attempt to broadcast the signed transaction with retry
        try:
            btc_tx_id = send_raw_transaction(request.signed_btc_tx)
        except Exception as e:
            logger.error(f"Failed to broadcast transaction after retries: {str(e)}")
            raise HTTPException(status_code=400, detail=f"Failed to broadcast transaction: {str(e)}")
        
        # Create new transaction record
        transaction = create_transaction(db, btc_tx_id, request.ethereum_address, None)
        update_transaction_status(db, transaction, TransactionStatus.BROADCASTED)
        
        return {"btc_tx_id": btc_tx_id, "message": "Wrap initiated, transaction broadcasted"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error in initiate_wrap: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {str(e)}")
    finally:
        db.close()

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((BrokenPipeError, http.client.RemoteDisconnected))
)
def send_raw_transaction(signed_tx):
    btc_rpc = load_btc_wallet()
    return btc_rpc.sendrawtransaction(signed_tx)

def retry_with_backoff(func, max_retries=3, initial_delay=2, backoff_factor=2):
    retries = 0
    while retries < max_retries:
        try:
            return func()
        except Exception as e:
            wait = initial_delay * (backoff_factor ** retries)
            logger.warning(f"Retry {retries + 1}/{max_retries} failed: {str(e)}. Waiting {wait} seconds.")
            time.sleep(wait)
            retries += 1
    raise Exception("Max retries reached")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((BrokenPipeError, http.client.RemoteDisconnected, ConnectionResetError))
)
def get_btc_transaction(tx_id: str):
    btc_rpc = load_btc_wallet()
    try:
        return btc_rpc.gettransaction(tx_id)
    except rpc.JSONRPCError as e:
        if e.error['code'] == -5:  # Transaction not in mempool
            logger.warning(f"Transaction {tx_id} not found in mempool: {str(e)}")
            return None
        else:
            logger.error(f"Error fetching transaction {tx_id}: {str(e)}")
            raise
    except (BrokenPipeError, http.client.RemoteDisconnected, ConnectionResetError) as e:
        logger.warning(f"Connection error while fetching transaction {tx_id}: {str(e)}")
        raise  # Raise the exception to trigger a retry
    except Exception as e:
        logger.error(f"Unexpected error fetching transaction {tx_id}: {str(e)}")
        raise

def process_pending_transactions():
    db = SessionLocal()
    try:
        pending_transactions = db.query(Transaction).filter(
            Transaction.status.in_([TransactionStatus.BROADCASTED, TransactionStatus.CONFIRMING])
        ).all()
        
        for transaction in pending_transactions:
            # print details of transactions
            print(f"checking pending Transaction {transaction.id}: {transaction.btc_tx_id}, {transaction.eth_address}, {transaction.status}")
            monitor_btc_transaction(transaction.id)
    except Exception as e:
        logger.error(f"Error processing pending transactions: {str(e)}")
    finally:
        db.close()

def monitor_btc_transaction(transaction_id: int):
    db = SessionLocal()
    try:
        transaction = db.get(Transaction, transaction_id)
        if not transaction:
            logger.error(f"Transaction {transaction_id} not found")
            return

        if transaction.status == TransactionStatus.COMPLETED:
            logger.info(f"Transaction {transaction_id} already completed")
            return

        if transaction.status == TransactionStatus.BROADCASTED:
            update_transaction_status(db, transaction, TransactionStatus.CONFIRMING)

        required_confirmations = 3
        tx_info = get_btc_transaction(transaction.btc_tx_id)
        if tx_info is None:
            logger.warning(f"Transaction {transaction_id} not found in mempool")
            return

        confirmations = tx_info.get('confirmations', 0)
        if confirmations >= required_confirmations:
            # Verify the transaction details
            amount = verify_btc_transaction(tx_info, transaction.btc_tx_id)
            transaction.amount = amount
            db.commit()
            
            # Initiate WBTC minting
            amount_wei = int(amount * 10**8)
            mint_wbtc(transaction.id, transaction.eth_address, amount_wei)
    except Exception as e:
        logger.error(f"Error monitoring transaction {transaction_id}: {str(e)}")
    finally:
        db.close()

def verify_btc_transaction(tx_info: any, tx_id: str):
    bridge_address = os.getenv('BRIDGE_BTC_ADDRESS')
    details = tx_info['details']
    for detail in details:
        if detail['category'] == 'receive' and detail['address'] == bridge_address:
            return detail['amount']
    
    raise ValueError("Transaction does not have output to bridge's BTC address")

def mint_wbtc(transaction_id: int, ethereum_address: str, amount_wei: int):
    db = SessionLocal()
    print(f"Minting WBTC for transaction {transaction_id} with amount {amount_wei} to {ethereum_address}")
    try:
        transaction = db.get(Transaction, transaction_id)
        if not transaction or transaction.status != TransactionStatus.CONFIRMING:
            logger.warning(f"Transaction {transaction_id} not found or not in CONFIRMING state")
            return
        
        update_transaction_status(db, transaction, TransactionStatus.MINTING)
        
        owner_address = os.getenv('OWNER_ADDRESS')
        nonce = w3.eth.get_transaction_count(owner_address)
        
        tx = wbtc_contract.functions.mint(
            ethereum_address,
            amount_wei
        ).build_transaction({
            'from': owner_address,
            'nonce': nonce,
            'gas': 2000000,
            'gasPrice': w3.eth.gas_price,
        })
        
        signed_tx = w3.eth.account.sign_transaction(tx, os.getenv('OWNER_PRIVATE_KEY'))
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        transaction.eth_tx_hash = tx_hash.hex()
        db.commit()
        
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=600)
        
        if receipt['status'] == 1:
            update_transaction_status(db, transaction, TransactionStatus.COMPLETED)
            logger.info(f"WBTC minted successfully. Transaction hash: {tx_hash.hex()}")
        else:
            update_transaction_status(db, transaction, TransactionStatus.FAILED)
            logger.error(f"WBTC minting failed. Transaction hash: {tx_hash.hex()}")
        
    except Exception as e:
        update_transaction_status(db, transaction, TransactionStatus.FAILED)
        logger.error(f"Failed to mint WBTC: {str(e)}")
    finally:
        db.close()

@app.get("/wrap-status/{btc_tx_id}")
async def wrap_status(btc_tx_id: str):
    db = SessionLocal()
    try:
        transaction = db.query(Transaction).filter_by(btc_tx_id=btc_tx_id).first()
        if not transaction:
            raise HTTPException(status_code=404, detail="Transaction not found")
        
        return WrapStatus(
            btc_tx_id=transaction.btc_tx_id,
            status=transaction.status,
            amount=transaction.amount,
            ethereum_address=transaction.eth_address,
            created_at=transaction.created_at,
            updated_at=transaction.updated_at,
            eth_tx_hash=transaction.eth_tx_hash
        )
    finally:
        db.close()

# Set up the scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(
    process_pending_transactions,
    IntervalTrigger(minutes=2),
    id='process_pending_transactions',
    name='Process pending transactions every 2 minutes',
    replace_existing=True)
scheduler.start()

# Shut down the scheduler when exiting the app
atexit.register(lambda: scheduler.shutdown())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)