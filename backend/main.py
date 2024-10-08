from decimal import Decimal
import logging
from logging.handlers import RotatingFileHandler
import sys
import math
from typing import List
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from web3 import Web3
from bitcoinrpc.authproxy import AuthServiceProxy
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from contextlib import asynccontextmanager
import json
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import uvicorn
import asyncio
import binascii
from bitcoinrpc.authproxy import JSONRPCException
from enum import Enum
from eth_abi.codec import ABICodec
from eth_abi.registry import registry
from eth.vm.forks.arrow_glacier.transactions import ArrowGlacierTransactionBuilder as TransactionBuilder
from eth_utils import to_bytes, encode_hex
from web3.auto import w3
from datetime import datetime, timedelta
import time
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from web3.exceptions import InvalidAddress



MIN_AMOUNT = 1

# Add these constants near the top of the file
BTB_FEE = Decimal('0.01')
ETH_FEE_IN_WBTB = 100
TokenUnit = 100000000
MaxGasPrice = 400*10**9 # 200 Gwei
MAX_ATTEMPTS = 20
UNWRAP_GAS_LIMIT = 50000

# Add this constant near the top of the file
CONFIRMATIONS_REQUIRED = 1

# Add this constant near the top of the file, with the other constants
DUST_THRESHOLD = Decimal('0.00000546')

# Add this section to configure CORS
origins = [
    "http://localhost",  # Allow requests from localhost
    # Add other origins as needed
    "http://localhost:5173",
]

load_dotenv()

def setup_logging():
    # Create a logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # Create handlers
    file_handler = RotatingFileHandler('app.log', maxBytes=10*1024*1024, backupCount=5)
    console_handler = logging.StreamHandler(sys.stdout)

    # Set levels
    file_handler.setLevel(logging.DEBUG)
    console_handler.setLevel(logging.WARNING)

    # Create formatters and add it to handlers
    log_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(log_format)
    console_handler.setFormatter(log_format)

    # Add handlers to the logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Set APScheduler logger to WARNING
    logging.getLogger('apscheduler').setLevel(logging.WARNING)

# Call this function at the start of your main.py
setup_logging()

# Use this logger throughout your application
logger = logging.getLogger(__name__)

# Create an AsyncIOScheduler
scheduler = AsyncIOScheduler()



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    scheduler.start()
    yield
    # Shutdown
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # Allow requests from these origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods
    allow_headers=["*"],  # Allow all headers
)

# Ethereum setup
w3 = Web3(Web3.HTTPProvider(os.getenv("ETH_NODE_URL")))
wbtb_address = os.getenv("WBTB_ADDRESS")

# Read the ABI from a JSON file instead of the Solidity file
abi_file_path = Path("../artifacts/contracts/WBTB.sol/WBTB.json")
if abi_file_path.exists():
    with open(abi_file_path, "r") as file:
        contract_abi = json.load(file)['abi']
else:
    raise FileNotFoundError(f"ABI file not found: {abi_file_path}")

# Create the contract instance
compiled_sol = w3.eth.contract(address=wbtb_address, abi=contract_abi)

# Bitbi setup
btb_node_url = os.getenv("BTB_NODE_URL")
btb_wallet_name = os.getenv('BTB_WALLET_NAME')

rpc_connection = AuthServiceProxy(btb_node_url)
# Load the wallet
try:
    rpc_connection.loadwallet(btb_wallet_name)
except Exception as e:
    logger.error(f"Failed to load wallet: {e}")

# Instead of creating a new AuthServiceProxy, update the existing one
btb_node_wallet_url = f"{btb_node_url}/wallet/{btb_wallet_name}"

bridge_btb_address = os.getenv('BRIDGE_BTB_ADDRESS')


# Database setup
engine = create_engine(os.getenv("DATABASE_URL"))
Base = declarative_base()
Session = sessionmaker(bind=engine)

class TransactionStatus(str, Enum):
    # Wrap statuses
    WRAP_BTB_TRANSACTION_BROADCASTED = "WRAP_BTB_TRANSACTION_BROADCASTED"
    WRAP_BTB_TRANSACTION_CONFIRMING = "WRAP_BTB_TRANSACTION_CONFIRMING"
    WBTB_MINTING_IN_PROGRESS = "WBTB_MINTING_IN_PROGRESS"
    WRAP_COMPLETED = "WRAP_COMPLETED"

    # Unwrap statuses
    UNWRAP_ETH_TRANSACTION_INITIATED = "UNWRAP_ETH_TRANSACTION_INITIATED"
    UNWRAP_ETH_TRANSACTION_CONFIRMING = "UNWRAP_ETH_TRANSACTION_CONFIRMING"
    UNWRAP_ETH_TRANSACTION_CONFIRMED = "UNWRAP_ETH_TRANSACTION_CONFIRMED"
    WBTB_BURN_CONFIRMED = "WBTB_BURN_CONFIRMED"
    UNWRAP_BTB_TRANSACTION_CREATING = "UNWRAP_BTB_TRANSACTION_CREATING"
    UNWRAP_BTB_TRANSACTION_BROADCASTED = "UNWRAP_BTB_TRANSACTION_BROADCASTED"
    UNWRAP_COMPLETED = "UNWRAP_COMPLETED"

    # Failure statuses
    FAILED_INSUFFICIENT_AMOUNT = "FAILED_INSUFFICIENT_AMOUNT"
    FAILED_TRANSACTION_NOT_FOUND = "FAILED_TRANSACTION_NOT_FOUND"
    FAILED_INSUFFICIENT_FUNDS = "FAILED_INSUFFICIENT_FUNDS"
    FAILED_TRANSACTION_UNKNOWN = "FAILED_TRANSACTION_UNKNOWN"
    FAILED_TRANSACTION_MAX_ATTEMPTS = "FAILED_TRANSACTION_MAX_ATTEMPTS"
class WrapTransaction(Base):
    __tablename__ = "wrap_transactions"
    id = Column(Integer, primary_key=True, index=True)
    btb_tx_id = Column(String, unique=True, index=True)
    wallet_id = Column(String, index=True)
    receiving_address = Column(String)
    amount = Column(Float)
    status = Column(String, default=TransactionStatus.WRAP_BTB_TRANSACTION_BROADCASTED)
    eth_tx_hash = Column(String)
    minted_wbtb_amount = Column(Float)  # New column
    
    exception_details = Column(Text, default='{}')
    exception_count = Column(Integer, default=0)
    last_exception_time = Column(DateTime, nullable=True)
    create_time = Column(DateTime, default=datetime.utcnow, nullable=False)

class UnwrapTransaction(Base):
    __tablename__ = "unwrap_transactions"
    id = Column(Integer, primary_key=True, index=True)
    eth_tx_hash = Column(String, unique=True, index=True)
    wallet_id = Column(String, index=True, nullable=True)
    btb_receiving_address = Column(String, nullable=True)
    amount = Column(Float, nullable=True)
    status = Column(String, default=TransactionStatus.UNWRAP_ETH_TRANSACTION_INITIATED)
    btb_tx_id = Column(String, nullable=True)
    eth_sender = Column(String, nullable=True)
    sent_btb_amount = Column(Float, nullable=True)  # New column
    # Existing columns
    exception_details = Column(Text, default='{}')
    exception_count = Column(Integer, default=0)
    last_exception_time = Column(DateTime, nullable=True)
    create_time = Column(DateTime, default=datetime.utcnow, nullable=False)

Base.metadata.create_all(bind=engine)

class WrapRequest(BaseModel):
    signed_btb_tx: str

class UnwrapRequest(BaseModel):
    signed_eth_tx: str

@app.post("/initiate-wrap/")
async def initiate_wrap(wrap_request: WrapRequest):
    try:
        logger.info(f"received signed btb tx: {wrap_request.signed_btb_tx}")
        rpc_connection = AuthServiceProxy(btb_node_wallet_url)
        # Decode and extract information from the signed Bitbi transaction
        decoded_tx = rpc_connection.decoderawtransaction(wrap_request.signed_btb_tx)
        logger.info(f"decoded tx: {decoded_tx}")
        op_return_data = next(output['scriptPubKey']['asm'] for output in decoded_tx['vout'] if output['scriptPubKey']['type'] == 'nulldata')
        logger.info(f"op return data: {op_return_data}")
        # remove the 'OP_RETURN ' prefix
        op_return_data = op_return_data.replace('OP_RETURN ', '')
        # reverse binascii.hexlify
        op_return_data = binascii.unhexlify(op_return_data).decode()
        logger.info(f"op return data: {op_return_data}")
        wallet_id, receiving_address = op_return_data.split(':')[1].split('-')
        if not receiving_address.startswith('0x'):
            receiving_address = '0x' + receiving_address
        logger.info(f"wallet id: {wallet_id}")
        logger.info(f"receiving address: {receiving_address}")
        amount = sum(output['value'] for output in decoded_tx['vout'] 
                     if output['scriptPubKey'].get('address') == bridge_btb_address)
        logger.info(f"Amount sent to bridge address: {amount} btb")
        if amount < MIN_AMOUNT:
            raise Exception(f"Amount sent to bridge address is less than the minimum amount of {MIN_AMOUNT} btb")
        
        # Broadcast the transaction
        btb_tx_id = rpc_connection.sendrawtransaction(wrap_request.signed_btb_tx)

        # Create a database record
        session = Session()
        new_wrap = WrapTransaction(
            btb_tx_id=btb_tx_id,
            wallet_id=wallet_id,
            receiving_address=receiving_address,
            amount=amount,
            status=TransactionStatus.WRAP_BTB_TRANSACTION_BROADCASTED
        )
        session.add(new_wrap)
        session.commit()
        session.close()

        return {"btb_tx_id": btb_tx_id, "status": TransactionStatus.WRAP_BTB_TRANSACTION_BROADCASTED}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/initiate-unwrap/")
async def initiate_unwrap(unwrap_request: UnwrapRequest):
    try:
        # Convert the hex string to bytes
        signed_tx_as_bytes = to_bytes(hexstr=unwrap_request.signed_eth_tx)

        # Decode the transaction
        decoded_tx = TransactionBuilder().decode(signed_tx_as_bytes)
        logger.info(f"decoded_tx: {decoded_tx}")
        
        # Convert the to_address bytes to a checksum address
        to_address = w3.to_checksum_address(decoded_tx.to)
        logger.info(f"to_address: {to_address}")
        
        # Convert the wbtb_address to a checksum address
        wbtb_checksum_address = w3.to_checksum_address(wbtb_address)
        
        if to_address != wbtb_checksum_address:
            raise ValueError(f"Transaction is not to the right WBTB contract. current to_address: {to_address}, expected: {wbtb_checksum_address}")
        
        
        # Extract the input data (which contains the function call and arguments)
        input_data = decoded_tx.data

        # The first 4 bytes are the function selector
        function_selector = input_data[:4]

        # Check if it's the burn function
        burn_selector = w3.keccak(text="burn(uint256,bytes)")[:4]

        if function_selector == burn_selector:
            # Decode the arguments
            decoded_args = w3.eth.contract(abi=[{
                "inputs": [
                    {"type": "uint256", "name": "amount"},
                    {"type": "bytes", "name": "data"}
                ],
                "name": "burn",
                "type": "function"
            }]).decode_function_input(input_data)

            amount, data = decoded_args[1]['amount'], decoded_args[1]['data']
            
            # Convert amount from satoshis to BTB
            amount_btb = amount / 1e8
            
            # Decode the data (assuming it's UTF-8 encoded)
            decoded_data = data.decode('utf-8')
            
            logger.info(f"Burn amount: {amount_btb} BTB")
            logger.info(f"Burn data: {decoded_data}")
            wallet_id, btb_receiving_address = decoded_data.split(':')[1].split('-')

            # Extract the 'from' address from the decoded transaction
            eth_sender = w3.to_checksum_address(decoded_tx.sender)
            logger.info(f"eth_sender: {eth_sender}")

            # Broadcast the transaction
            eth_tx_hash = None
            try:
                eth_tx_hash = w3.eth.send_raw_transaction(signed_tx_as_bytes)
                eth_tx_hash = w3.to_hex(eth_tx_hash)
            except Exception as e:
                print("type(Exception):", type(e))
                if "already known" in str(e):
                    # The transaction is already in the mempool
                    # We can extract the transaction hash from the signed transaction
                    eth_tx_hash = w3.to_hex(w3.keccak(signed_tx_as_bytes))
                    logger.info(f"Transaction already in mempool: {eth_tx_hash}")
                else:
                    # If it's a different error, re-raise it
                    raise
            
            # Create a database record
            session = Session()
            new_unwrap = UnwrapTransaction(
                eth_tx_hash=eth_tx_hash,
                status=TransactionStatus.UNWRAP_ETH_TRANSACTION_INITIATED,
                amount=amount_btb,
                wallet_id=wallet_id,
                btb_receiving_address=btb_receiving_address,
                eth_sender=eth_sender  # Add the eth_sender to the new record
            )
            session.add(new_unwrap)
            session.commit()
            session.close()

            return {"eth_tx_hash": eth_tx_hash, "status": TransactionStatus.UNWRAP_ETH_TRANSACTION_INITIATED}
        else:
            logger.warning("Transaction is not a burn function call")
            raise ValueError("Transaction is not a burn function call")

    except Exception as e:
        logger.error(f"Error in initiate_unwrap: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/wrap-status/{btb_tx_id}")
async def wrap_status(btb_tx_id: str):
    session = Session()
    wrap_tx = session.query(WrapTransaction).filter(WrapTransaction.btb_tx_id == btb_tx_id).first()
    session.close()

    if not wrap_tx:
        raise HTTPException(status_code=404, detail="Wrap transaction not found")

    return {"status": wrap_tx.status, "eth_tx_hash": wrap_tx.eth_tx_hash}

@app.get("/unwrap-status/{eth_tx_hash}")
async def unwrap_status(eth_tx_hash: str):
    session = Session()
    unwrap_tx = session.query(UnwrapTransaction).filter(UnwrapTransaction.eth_tx_hash == eth_tx_hash).first()
    session.close()

    if not unwrap_tx:
        raise HTTPException(status_code=404, detail="Unwrap transaction not found")

    return {"status": unwrap_tx.status, "btb_tx_id": unwrap_tx.btb_tx_id}

@app.get("/wrap-history/{wallet_id}")
async def wrap_history(wallet_id: str):
    session = Session()
    wrap_txs: List[WrapTransaction] = session.query(WrapTransaction).filter(WrapTransaction.wallet_id == wallet_id).all()
    session.close()

    return [{"btb_tx_id": tx.btb_tx_id, "status": tx.status, "amount": tx.amount, "eth_tx_hash": tx.eth_tx_hash, "receiving_address": tx.receiving_address, "exception_details": tx.exception_details, "exception_count": tx.exception_count, "minted_wbtb_amount": tx.minted_wbtb_amount, "last_exception_time": tx.last_exception_time, "create_time": tx.create_time} for tx in wrap_txs]

@app.get("/unwrap-history/{wallet_id}")
async def unwrap_history(wallet_id: str):
    session = Session()
    unwrap_txs: List[UnwrapTransaction] = session.query(UnwrapTransaction).filter(UnwrapTransaction.wallet_id == wallet_id).all()
    session.close()

    return [{
        "eth_tx_hash": tx.eth_tx_hash,
        "status": tx.status,
        "amount": tx.amount,
        "btb_tx_id": tx.btb_tx_id,
        "btb_receiving_address": tx.btb_receiving_address,
        "sent_btb_amount": tx.sent_btb_amount,  # Add this line
        "exception_details": tx.exception_details,
        "exception_count": tx.exception_count,
        "last_exception_time": tx.last_exception_time,
        "create_time": tx.create_time,
        "eth_sender": tx.eth_sender
    } for tx in unwrap_txs]

@app.get("/bridge-info")
async def get_bridge_info():
    try:
        # Get WBTB contract ABI
        wbtb_abi = contract_abi

        # Get wrap fee
        wrap_fee = {
            "btb_fee": float(BTB_FEE),
            "eth_fee_in_wbtb": ETH_FEE_IN_WBTB
        }

        # Get unwrap fee
        unwrap_fee = {
            "btb_fee": float(BTB_FEE),
            "eth_fee": round(w3.eth.gas_price / (10**9) * UNWRAP_GAS_LIMIT / (10**9), 6),
            "eth_gas_price": str(w3.eth.gas_price),
            "eth_gas_limit": UNWRAP_GAS_LIMIT,
        }

        # Minimum amount of WBTB to wrap
        min_wrap_amount = 10_000

        # Combine all information
        bridge_info = {
            "wbtb_contract_abi": wbtb_abi,
            "wrap_fee": wrap_fee,
            "unwrap_fee": unwrap_fee,
            "min_wrap_amount": min_wrap_amount,
            "bridge_btb_address": bridge_btb_address,
            "wbtb_contract_address": wbtb_address,
            "eth_chain_id": w3.eth.chain_id
        }

        return JSONResponse(content=bridge_info)
    except Exception as e:
        logger.error(f"Error getting bridge info: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Add these new endpoints
@app.get("/unwrap-eth-transaction-count/{address}")
async def get_unwrap_eth_transaction_count(address: str):
    try:
        # Ensure the address is valid
        if not w3.is_address(address):
            raise HTTPException(status_code=400, detail="Invalid Ethereum address")
        
        # Get the transaction count (nonce) from the Ethereum network
        eth_nonce = w3.eth.get_transaction_count(address)
        print("eth_nonce:", eth_nonce)
        # Get the count of unwrap transactions for this address
        session = Session()
        unwrap_count = session.query(UnwrapTransaction).filter(UnwrapTransaction.eth_sender == address).count()
        session.close()
        
        # Use the maximum of eth_nonce and unwrap_count as the final nonce
        final_nonce = max(eth_nonce, unwrap_count)
        
        return {
            "address": address,
            "eth_transaction_count": eth_nonce,
            "unwrap_transaction_count": unwrap_count,
            "final_nonce": final_nonce,
            "chain_id": w3.eth.chain_id,
        }
    except Exception as e:
        logger.error(f"Error getting transaction count for address {address}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Add this new endpoint near the other endpoint definitions
@app.get("/bridge-addresses")
async def get_bridge_addresses():
    return {
        "btb_bridge_address": bridge_btb_address,
        "eth_bridge_contract_address": wbtb_address
    }

# Add this new endpoint
@app.get("/eth-address/{address}/balance")
async def get_eth_address_balance(address: str):
    try:
        # Validate the Ethereum address
        if not w3.is_address(address):
            raise HTTPException(status_code=400, detail="Invalid Ethereum address")

        # Convert to checksum address
        checksum_address = w3.to_checksum_address(address)

        # Get ETH balance
        eth_balance = w3.eth.get_balance(checksum_address)
        eth_balance_in_ether = w3.from_wei(eth_balance, 'ether')

        # Get WBTB balance
        wbtb_balance = compiled_sol.functions.balanceOf(checksum_address).call()
        wbtb_balance_in_btb = wbtb_balance / 1e8  # Convert from satoshis to BTB

        return {
            "address": checksum_address,
            "eth_balance": float(eth_balance_in_ether),
            "wbtb_balance": float(wbtb_balance_in_btb)
        }
    except InvalidAddress:
        raise HTTPException(status_code=400, detail="Invalid Ethereum address")
    except Exception as e:
        logger.error(f"Error getting balance for address {address}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Background tasks (to be run periodically)

def update_exception_details(tx, exception):
    """
    Update exception details and count for a transaction.
    
    :param tx: The transaction object (WrapTransaction or UnwrapTransaction)
    :param exception: The exception that occurred
    """
    exception_str = str(exception)
    
    # Load existing exception details
    exception_details = json.loads(tx.exception_details or '{}')
    
    # Update the count
    exception_details[exception_str] = exception_details.get(exception_str, 0) + 1
    
    # Save updated exception details
    tx.exception_details = json.dumps(exception_details)
    tx.exception_count = min(sum(exception_details.values()), MAX_ATTEMPTS)  # Cap at 20 or another suitable maximum
    tx.last_exception_time = datetime.utcnow()

    if tx.exception_count == MAX_ATTEMPTS:
        tx.status = TransactionStatus.FAILED_TRANSACTION_MAX_ATTEMPTS
        logger.error(f"{'wrap' if isinstance(tx, WrapTransaction) else 'unwrap'} Transaction {tx.btb_tx_id if isinstance(tx, WrapTransaction) else tx.eth_tx_hash} failed after {MAX_ATTEMPTS} attempts")

def reset_exception_details(tx):
    """
    Reset exception details, count, and last_exception_time for a transaction.
    
    :param tx: The transaction object (WrapTransaction or UnwrapTransaction)
    """
    tx.exception_details = '{}'
    tx.exception_count = 0
    tx.last_exception_time = None

def should_process_transaction(tx):
    """
    Determine if a transaction should be processed based on exponential backoff.
    
    :param tx: The transaction object (WrapTransaction or UnwrapTransaction)
    :return: Boolean indicating whether the transaction should be processed
    """
    if tx.last_exception_time is None:
        return True
    
    # Add a maximum backoff time (e.g., 1 day)
    max_backoff_minutes = 24 * 60  # 1 day in minutes
    
    backoff_time = min(2 ** tx.exception_count, max_backoff_minutes)  # Exponential backoff with a maximum
    next_process_time = tx.last_exception_time + timedelta(minutes=backoff_time)
    logger.info(f"""id: {tx.id} eth_tx_hash: {tx.eth_tx_hash} next_process_time: {next_process_time} current_time: {datetime.utcnow()} 
                backoff_time: {backoff_time} tx.last_exception_time: {tx.last_exception_time} 
                timedelta(minutes=backoff_time): {timedelta(minutes=backoff_time)}""")
    
    return datetime.utcnow() >= next_process_time

async def process_wrap_transactions():
    rpc_connection = AuthServiceProxy(btb_node_wallet_url)

    session = Session()
    broadcasted_txs = session.query(WrapTransaction).filter(WrapTransaction.status == TransactionStatus.WRAP_BTB_TRANSACTION_BROADCASTED).all()

    for tx in broadcasted_txs:
        if not should_process_transaction(tx):
            continue
        
        try:
            # Check Bitbi transaction confirmation
            btb_tx = rpc_connection.gettransaction(tx.btb_tx_id)
            if btb_tx['confirmations'] >= CONFIRMATIONS_REQUIRED:
                # Convert BTB amount to satoshis, then to Wei
                satoshis = int(tx.amount * TokenUnit)  # 1 BTB = 100,000,000 satoshis
                # Deduct the ETH fee in WBTB
                satoshis -= ETH_FEE_IN_WBTB * TokenUnit
                
                # Record the actual minted WBTB amount
                tx.minted_wbtb_amount = satoshis / TokenUnit

                # Mint WBTB
                gas_price = int(w3.eth.gas_price * 1.1)
                if gas_price > MaxGasPrice:
                    gas_price = MaxGasPrice

                logger.info(f"minting gas_price: {gas_price}")

                nonce = w3.eth.get_transaction_count(os.getenv("OWNER_ADDRESS"))
                chain_id = w3.eth.chain_id  # Get the current chain ID
                
                if not tx.receiving_address.startswith('0x'):
                    tx.receiving_address = '0x' + tx.receiving_address
                # Convert the receiving address to checksum format
                checksum_receiving_address = w3.to_checksum_address(tx.receiving_address)
                
                mint_tx = compiled_sol.functions.mint(checksum_receiving_address, satoshis).build_transaction({
                    'chainId': chain_id,
                    'gasPrice': int(gas_price),
                    'nonce': nonce,
                    'gas': 100000
                })
                
                logger.info(f"Mint transaction: {mint_tx}")
                signed_tx = w3.eth.account.sign_transaction(mint_tx, os.getenv("OWNER_PRIVATE_KEY"))
                eth_tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

                tx.status = TransactionStatus.WBTB_MINTING_IN_PROGRESS
                tx.eth_tx_hash = eth_tx_hash.hex()

                # If successful, reset exception details
                reset_exception_details(tx)
        except Exception as e:
            logger.error(f"process_wrap_transactions error for transaction {tx.btb_tx_id}: {e}")
            update_exception_details(tx, e)
            continue

    session.commit()

    minting_txs = session.query(WrapTransaction).filter(WrapTransaction.status == TransactionStatus.WBTB_MINTING_IN_PROGRESS).all()

    for tx in minting_txs:
        if not should_process_transaction(tx):
            continue
        try:
            # Check WBTB minting transaction confirmation
            eth_tx = w3.eth.get_transaction_receipt(tx.eth_tx_hash)
            if eth_tx and eth_tx['status'] == 1:
                tx.status = TransactionStatus.WRAP_COMPLETED
            else:
                logger.error(f"Transaction {tx.eth_tx_hash} failed with status 0")
                tx.status = TransactionStatus.FAILED_TRANSACTION_UNKNOWN
            reset_exception_details(tx)
        except Exception as e:
            logger.error(f"wrap Error processing transaction eth_tx_hash: {tx.eth_tx_hash}, error: {e}")
            update_exception_details(tx, e)
            continue

    session.commit()
    session.close()


async def process_unwrap_transactions():
    session = Session()
    initiated_txs = session.query(UnwrapTransaction).filter(UnwrapTransaction.status == TransactionStatus.UNWRAP_ETH_TRANSACTION_INITIATED).all()
    rpc_connection = AuthServiceProxy(btb_node_wallet_url)
    
    for tx in initiated_txs:
        if not should_process_transaction(tx):
            continue
        
        try:
            eth_tx_receipt = w3.eth.get_transaction_receipt(tx.eth_tx_hash)
            if eth_tx_receipt:
                logger.info(f"eth_tx_receipt of {tx.eth_tx_hash}: {eth_tx_receipt}")
                if eth_tx_receipt['status'] == 1:
                    eth_tx = w3.eth.get_transaction(tx.eth_tx_hash)
                    calldata = eth_tx['input']
                    logger.info(f"calldata: {calldata}")
                    
                    # Get the function signature (first 4 bytes of the calldata)
                    func_signature = calldata[:4]
                    burn_signature = w3.keccak(text="burn(uint256,bytes)")[:4]
                    logger.info(f"Extracted function signature: {func_signature}")
                    logger.info(f"Expected burn function signature: {burn_signature}")
                    
                    if func_signature == burn_signature:
                        # Decode the burn function parameters
                        decoded_input = compiled_sol.decode_function_input(calldata)
                        logger.info(f"decoded_input: {decoded_input}")
                        logger.info(f"decoded_input[1]: {decoded_input[1]}")
                        burnt_amount = decoded_input[1]['amount']
                        logger.info(f"burnt_amount: {burnt_amount}")
                        amount = burnt_amount / 100000000
                        # check if amount is less than MIN_AMOUNT or amount is not a number
                        if amount < MIN_AMOUNT or math.isnan(amount):
                            tx.status = TransactionStatus.FAILED_INSUFFICIENT_AMOUNT
                            logger.error(f"Amount sent to bridge address is less than the minimum amount of {MIN_AMOUNT} BTB, eth_tx_hash: {tx.eth_tx_hash}")
                            continue

                        # Extract wallet_id and btb_receiving_address from the _data parameter
                        data_param = decoded_input[1]['data'].decode('utf-8')
                        wallet_id, btb_receiving_address = data_param.split(':')[1].split('-')

                        # Update the database record with extracted information
                        tx.wallet_id = wallet_id
                        tx.btb_receiving_address = btb_receiving_address
                        tx.amount = amount
                        tx.status = TransactionStatus.UNWRAP_ETH_TRANSACTION_CONFIRMING
                    else:
                        logger.error(f"Unexpected function call: {func_signature}")
                        continue
                elif eth_tx_receipt['status'] == 0:
                    logger.error(f"Unwrap transaction {tx.eth_tx_hash} failed with status 0")
                    logger.info(f"Failed transaction receipt: {eth_tx_receipt}")
                    
                    transaction = w3.eth.get_transaction(tx.eth_tx_hash)
                    logger.info(f"Failed transaction details: {transaction}")
                    
                    try:
                        result = w3.eth.call(
                            {
                                'to': transaction['to'],
                                'from': transaction['from'],
                                'data': transaction['input'],
                                'value': transaction['value'],
                                'gas': transaction['gas'],
                                'gasPrice': transaction['gasPrice'],
                            },
                            block_identifier=eth_tx_receipt['blockNumber'] - 1
                        )
                    except Exception as call_exception:
                        revert_reason = str(call_exception)
                        logger.error(f"Revert reason: {revert_reason}")
                        
                        # Check if it's an InsufficientBalance error
                        if "0xe450d38c" in revert_reason:
                            tx.status = TransactionStatus.FAILED_INSUFFICIENT_FUNDS
                            error_message = "Insufficient balance for unwrap"
                        else:
                            tx.status = TransactionStatus.FAILED_TRANSACTION_UNKNOWN
                            error_message = "Unknown error occurred"
                        
                        tx.exception_details = json.dumps({
                            "error": error_message,
                            # "receipt": str(eth_tx_receipt),
                            "revert_reason": revert_reason
                        })
                    continue

                # If successful, reset exception details
                reset_exception_details(tx)
        except Exception as e:
            logger.error(f"unwrap Error processing transaction eth_tx_hash: {tx.eth_tx_hash}, error: {e}")
            update_exception_details(tx, e)
            continue

    session.commit()

    confirming_txs = session.query(UnwrapTransaction).filter(UnwrapTransaction.status == TransactionStatus.UNWRAP_ETH_TRANSACTION_CONFIRMING).all()

    for tx in confirming_txs:
        if not should_process_transaction(tx):
            continue
        
        try:
            eth_tx_receipt = w3.eth.get_transaction_receipt(tx.eth_tx_hash)
            # Check the number of confirmations
            block_number = eth_tx_receipt['blockNumber']
            current_block = w3.eth.block_number
            confirmations = current_block - block_number
            if confirmations >= CONFIRMATIONS_REQUIRED:
                tx.status = TransactionStatus.UNWRAP_ETH_TRANSACTION_CONFIRMED
            else:
                logger.info(f"Transaction {tx.eth_tx_hash} not yet confirmed")
            reset_exception_details(tx)
        except Exception as e:
            logger.error(f"unwrap Error processing transaction eth_tx_hash: {tx.eth_tx_hash}, error: {e}")
            update_exception_details(tx, e)
            continue

    session.commit()

    confirmed_txs = session.query(UnwrapTransaction).filter(UnwrapTransaction.status == TransactionStatus.UNWRAP_ETH_TRANSACTION_CONFIRMED).all()

    for tx in confirmed_txs:
        if not should_process_transaction(tx):
            continue
        
        try:
            # Fetch unspent transactions to use as inputs
                
            bridge_address = os.getenv('BRIDGE_BTB_ADDRESS')
            amount_to_send = Decimal(str(tx.amount)) - BTB_FEE
            total_needed = amount_to_send + BTB_FEE

            # Fetch unspent UTXOs with minimum amount and sum
            unspent = rpc_connection.listunspent(0, 999999999, [bridge_address], False, {
                "minimumAmount": float(DUST_THRESHOLD),
                "minimumSumAmount": float(total_needed)
            })
            if tx.eth_tx_hash == "0x4aeab91cf04675a2589db1b51710984283ef570403d591dfa4719da2a2a4b317":
                logger.info(f"unspent: {unspent} total_needed: {total_needed} bridge_address: {bridge_address}")
                
            if not unspent:
                logger.error(f"No unspent transactions found for total_needed: {total_needed} and bridge_address: {bridge_address}")
                raise Exception("No unspent transactions found")

            # Calculate total available amount
            total_amount = sum(Decimal(str(input['amount'])) for input in unspent)

            if total_amount < total_needed:
                logger.error(f"Not enough funds to create the transaction total_amount: {total_amount} total_needed: {total_needed}")
                raise Exception("Not enough funds to create the transaction")

            # Prepare inputs and outputs
            tx_inputs = [{"txid": input['txid'], "vout": input['vout']} for input in unspent]
            tx_outputs = {
                tx.btb_receiving_address: float(amount_to_send),
            }

            # Calculate change and add it if it's not dust
            change = total_amount - total_needed
            if change > DUST_THRESHOLD:  # Minimum non-dust output
                tx_outputs[bridge_address] = float(change)

            logger.info(f"tx_outputs: {tx_outputs}")

            # Record the actual sent BTB amount
            tx.sent_btb_amount = float(amount_to_send)
            logger.info(f"Sent BTB amount: {tx.sent_btb_amount}")
            wbtb_receive_address = tx.btb_receiving_address
            if wbtb_receive_address.startswith('0x'):
                wbtb_receive_address = wbtb_receive_address[2:]
            # Format OP_RETURN data as hexadecimal
            op_return_data = f"un:{tx.wallet_id}-{wbtb_receive_address}"
            logger.info(f"op_return_data: {op_return_data}")

            op_return_hex = binascii.hexlify(op_return_data.encode()).decode()
            tx_outputs["data"] = op_return_hex
            logger.info(f"tx_outputs: {tx_outputs}")
            # Create raw transaction
            btb_tx = rpc_connection.createrawtransaction(tx_inputs, tx_outputs)

            signed_tx = rpc_connection.signrawtransactionwithwallet(btb_tx)
            btb_tx_id = rpc_connection.sendrawtransaction(signed_tx['hex'])

            tx.status = TransactionStatus.UNWRAP_BTB_TRANSACTION_BROADCASTED
            tx.btb_tx_id = btb_tx_id

            # If successful, reset exception details
            reset_exception_details(tx)
        except Exception as e:
            logger.error(f"Error creating Bitbi transaction for {tx.eth_tx_hash}: {e}")
            update_exception_details(tx, e)
            continue

    session.commit()

    broadcasted_txs = session.query(UnwrapTransaction).filter(UnwrapTransaction.status == TransactionStatus.UNWRAP_BTB_TRANSACTION_BROADCASTED).all()

    for tx in broadcasted_txs:
        if not should_process_transaction(tx):
            continue
        
        try:
            btb_tx = rpc_connection.gettransaction(tx.btb_tx_id)
            if btb_tx['confirmations'] >= CONFIRMATIONS_REQUIRED:
                tx.status = TransactionStatus.UNWRAP_COMPLETED
            # If successful, reset exception details
            reset_exception_details(tx)

        except Exception as e:
            logger.error(f"Error checking Bitbi transaction {tx.btb_tx_id}: {e}")
            update_exception_details(tx, e)
            continue

    session.commit()
    session.close()

# Add jobs to the scheduler
scheduler.add_job(process_wrap_transactions, IntervalTrigger(minutes=2))
scheduler.add_job(process_unwrap_transactions, IntervalTrigger(minutes=2))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
