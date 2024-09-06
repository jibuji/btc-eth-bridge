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
from typing import Optional

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
btc_rpc = AuthServiceProxy(os.getenv('BTC_NODE_URL'))

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

logging.basicConfig(level=logging.INFO)
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
async def initiate_wrap(request: WrapRequest, background_tasks: BackgroundTasks):
    db = SessionLocal()
    try:
        # Broadcast the signed transaction
        btc_tx_id = btc_rpc.sendrawtransaction(request.signed_btc_tx)
        
        # Create new transaction record
        transaction = create_transaction(db, btc_tx_id, request.ethereum_address, None)
        update_transaction_status(db, transaction, TransactionStatus.BROADCASTED)
        
        # Schedule transaction monitoring
        background_tasks.add_task(monitor_btc_transaction, transaction.id)
        
        return {"btc_tx_id": btc_tx_id, "message": "Wrap initiated, transaction broadcasted"}
    except Exception as e:
        db.rollback()
        logger.error(f"Error in initiate_wrap: {str(e)}")
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")
    finally:
        db.close()

def retry_with_backoff(func, max_retries=3, initial_delay=1, backoff_factor=2):
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

def monitor_btc_transaction(transaction_id: int):
    db = SessionLocal()
    try:
        transaction = db.get(Transaction, transaction_id)
        if not transaction:
            logger.error(f"Transaction {transaction_id} not found")
            return

        update_transaction_status(db, transaction, TransactionStatus.CONFIRMING)

        required_confirmations = 3
        while True:
            try:
                tx_info = retry_with_backoff(lambda: btc_rpc.gettransaction(transaction.btc_tx_id))
                confirmations = tx_info.get('confirmations', 0)
                
                if confirmations >= required_confirmations:
                    # Verify the transaction details
                    amount = verify_btc_transaction(transaction.btc_tx_id)
                    transaction.amount = amount
                    db.commit()

                    # Initiate WBTC minting
                    amount_wei = int(amount * 10**8)
                    mint_wbtc(transaction.id, transaction.eth_address, amount_wei)
                    break
                
                time.sleep(60)  # Wait for 1 minute before checking again
            except Exception as e:
                logger.error(f"Error monitoring transaction {transaction_id}: {str(e)}")
                time.sleep(300)  # Wait for 5 minutes before retrying on error
    finally:
        db.close()

def verify_btc_transaction(tx_id: str):
    tx = btc_rpc.gettransaction(tx_id, True)
    bridge_address = os.getenv('BRIDGE_BTC_ADDRESS')
    
    for vout in tx['vout']:
        if 'scriptPubKey' in vout and 'addresses' in vout['scriptPubKey']:
            if bridge_address in vout['scriptPubKey']['addresses']:
                return vout['value']
    
    raise ValueError("Transaction does not have output to bridge's BTC address")

def mint_wbtc(transaction_id: int, ethereum_address: str, amount_wei: int):
    db = SessionLocal()
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

# Scheduler setup
scheduler = BackgroundScheduler()
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)