from fastapi import FastAPI, HTTPException, BackgroundTasks
from web3 import Web3
from pydantic import BaseModel
import os
from dotenv import load_dotenv
from bitcoinrpc.authproxy import AuthServiceProxy
from decimal import Decimal
from enum import Enum
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timedelta
from web3.exceptions import TimeExhausted
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import atexit

load_dotenv()

app = FastAPI()

# Database setup
engine = create_engine(os.getenv('DATABASE_URL'))
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class TransactionStatus(Enum):
    PENDING = "pending"
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
    status = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    eth_tx_hash = Column(String)  # New column for Ethereum transaction hash

Base.metadata.create_all(bind=engine)

# Web3 and Bitcoin RPC setup
w3 = Web3(Web3.HTTPProvider(os.getenv('ETH_NODE_URL')))
btc_rpc = AuthServiceProxy(os.getenv('BTC_NODE_URL'))

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

def verify_btc_transaction(tx_id: str, required_confirmations: int = 6):
    try:
        tx = btc_rpc.getrawtransaction(tx_id, True)
        confirmations = tx.get('confirmations', 0)
        if confirmations < required_confirmations:
            raise ValueError(f"Transaction needs {required_confirmations} confirmations, but has only {confirmations}")
        # Verify the transaction output is to the bridge's BTC address
        for vout in tx['vout']:
            if vout['scriptPubKey']['addresses'][0] == os.getenv('BRIDGE_BTC_ADDRESS'):
                return vout['value']  # Return the amount sent to the bridge
        raise ValueError("Transaction does not have output to bridge's BTC address")
    except Exception as e:
        raise ValueError(f"Failed to verify BTC transaction: {str(e)}")

def create_transaction(db, btc_tx_id, eth_address, amount):
    transaction = Transaction(
        btc_tx_id=btc_tx_id,
        eth_address=eth_address,
        amount=amount,
        status=TransactionStatus.PENDING.value
    )
    db.add(transaction)
    db.commit()
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
            if existing_transaction.status == TransactionStatus.COMPLETED.value:
                raise HTTPException(status_code=400, detail="Transaction already processed")
            elif existing_transaction.status == TransactionStatus.PENDING.value:
                return {"message": "Wrap already in progress"}
        
        # Verify the Bitcoin transaction
        btc_amount = verify_btc_transaction(request.btc_tx_id)
        
        # Create new transaction record
        transaction = create_transaction(db, request.btc_tx_id, request.ethereum_address, btc_amount)
        
        # Convert BTC amount to Wei
        amount_wei = int(btc_amount * 10**8)
        
        # Add minting task to background tasks
        background_tasks.add_task(mint_wbtc, transaction.id, request.ethereum_address, amount_wei)
        
        return {"message": "Wrap initiated, WBTC will be minted after confirmation"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()

def mint_wbtc(transaction_id: int, ethereum_address: str, amount_wei: int):
    db = SessionLocal()
    try:
        transaction = db.query(Transaction).get(transaction_id)
        if not transaction or transaction.status != TransactionStatus.PENDING.value:
            return
        
        tx = wbtc_contract.functions.mint(
            ethereum_address,
            amount_wei
        ).build_transaction({
            'from': os.getenv('OWNER_ADDRESS'),
            'nonce': w3.eth.get_transaction_count(os.getenv('OWNER_ADDRESS')),
        })
        
        signed_tx = w3.eth.account.sign_transaction(tx, os.getenv('OWNER_PRIVATE_KEY'))
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        # Update transaction with eth_tx_hash
        transaction.eth_tx_hash = tx_hash.hex()
        db.commit()
        
        try:
            # Wait for transaction receipt with a timeout
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=600)  # 10 minutes timeout
            
            # Check for sufficient confirmations
            current_block = w3.eth.block_number
            confirmations = current_block - receipt['blockNumber'] + 1
            
            if confirmations >= 6:
                update_transaction_status(db, transaction, TransactionStatus.COMPLETED.value)
                print(f"WBTC minted successfully. Transaction hash: {tx_hash.hex()}")
            else:
                # If not enough confirmations, we'll need to check again later
                update_transaction_status(db, transaction, TransactionStatus.PENDING_CONFIRMATIONS.value)
                print(f"WBTC minted, waiting for more confirmations. Current: {confirmations}")
        except TimeExhausted:
            update_transaction_status(db, transaction, TransactionStatus.PENDING_CONFIRMATIONS.value)
            print(f"Transaction sent but not yet mined. Hash: {tx_hash.hex()}")
    except Exception as e:
        update_transaction_status(db, transaction, TransactionStatus.FAILED.value)
        print(f"Failed to mint WBTC: {str(e)}")
    finally:
        db.close()

# New function to check pending confirmations
def check_pending_confirmations():
    db = SessionLocal()
    try:
        pending_txs = db.query(Transaction).filter_by(status=TransactionStatus.PENDING_CONFIRMATIONS.value).all()
        for tx in pending_txs:
            receipt = w3.eth.get_transaction_receipt(tx.eth_tx_hash)
            if receipt:
                current_block = w3.eth.block_number
                confirmations = current_block - receipt['blockNumber'] + 1
                if confirmations >= 6:
                    update_transaction_status(db, tx, TransactionStatus.COMPLETED.value)
                    print(f"Transaction {tx.eth_tx_hash} now has {confirmations} confirmations. Marked as completed.")
    finally:
        db.close()

scheduler = BackgroundScheduler()


#  run with each new Ethereum block:
def check_new_block():
    current_block = w3.eth.block_number
    if current_block > check_new_block.last_checked_block:
        check_pending_confirmations()
        check_new_block.last_checked_block = current_block

check_new_block.last_checked_block = w3.eth.block_number

scheduler.add_job(
    func=check_new_block,
    trigger=IntervalTrigger(seconds=15),  # Ethereum blocks are ~15 seconds apart
    id='check_new_block_job',
    name='Check for new Ethereum blocks',
    replace_existing=True)

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
        if not transaction or transaction.status != TransactionStatus.PENDING.value:
            return
        
        # Burn WBTC
        burn_tx_hash = burn_wbtc(ethereum_address, amount_wei)
        
        # Initiate BTC transfer
        btc_amount = amount_wei / 10**8
        btc_tx = btc_rpc.sendtoaddress(bitcoin_address, btc_amount)
        
        update_transaction_status(db, transaction, TransactionStatus.COMPLETED.value)
        print(f"Unwrap completed. ETH burn tx: {burn_tx_hash.hex()}, BTC tx: {btc_tx}")
    except Exception as e:
        update_transaction_status(db, transaction, TransactionStatus.FAILED.value)
        print(f"Failed to process unwrap: {str(e)}")
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
            Transaction.status == TransactionStatus.PENDING.value,
            Transaction.created_at < expiration_time
        ).all()
        for transaction in expired_transactions:
            update_transaction_status(db, transaction, TransactionStatus.EXPIRED.value)
        print(f"Expired {len(expired_transactions)} transactions")
    finally:
        db.close()

def reconcile_balances():
    # Implement balance reconciliation logic here
    pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)