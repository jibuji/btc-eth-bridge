from decimal import Decimal
import logging
from logging.handlers import RotatingFileHandler
import sys
import math
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from web3 import Web3
from bitcoinrpc.authproxy import AuthServiceProxy
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Float
from sqlalchemy.orm import declarative_base, sessionmaker
from contextlib import asynccontextmanager
import json
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import uvicorn
import asyncio
import binascii
import re
import time
from bitcoinrpc.authproxy import JSONRPCException
from enum import Enum

MIN_AMOUNT = 1000

# Add these constants near the top of the file
BTC_FEE = Decimal('0.01')
ETH_FEE_IN_WBTC = 100

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

# Ethereum setup
w3 = Web3(Web3.HTTPProvider(os.getenv("ETH_NODE_URL")))
wbtc_address = os.getenv("WBTC_ADDRESS")

# Read the ABI from a JSON file instead of the Solidity file
abi_file_path = Path("../artifacts/contracts/WBTC.sol/WBTC.json")
if abi_file_path.exists():
    with open(abi_file_path, "r") as file:
        contract_abi = json.load(file)['abi']
else:
    raise FileNotFoundError(f"ABI file not found: {abi_file_path}")

# Create the contract instance
compiled_sol = w3.eth.contract(address=wbtc_address, abi=contract_abi)

# Bitcoin setup
btc_node_url = os.getenv("BTC_NODE_URL")
btc_wallet_name = os.getenv('BTC_WALLET_NAME')

rpc_connection = AuthServiceProxy(btc_node_url)
# Load the wallet
try:
    rpc_connection.loadwallet(btc_wallet_name)
except Exception as e:
    logger.error(f"Failed to load wallet: {e}")

# Instead of creating a new AuthServiceProxy, update the existing one
btc_node_wallet_url = f"{btc_node_url}/wallet/{btc_wallet_name}"

bridge_btc_address = os.getenv('BRIDGE_BTC_ADDRESS')


# Database setup
engine = create_engine(os.getenv("DATABASE_URL"))
Base = declarative_base()
Session = sessionmaker(bind=engine)

class TransactionStatus(str, Enum):
    # Wrap statuses
    WRAP_BTC_TRANSACTION_BROADCASTED = "WRAP_BTC_TRANSACTION_BROADCASTED"
    WRAP_BTC_TRANSACTION_CONFIRMING = "WRAP_BTC_TRANSACTION_CONFIRMING"
    WBTC_MINTING_IN_PROGRESS = "WBTC_MINTING_IN_PROGRESS"
    WRAP_COMPLETED = "WRAP_COMPLETED"

    # Unwrap statuses
    UNWRAP_ETH_TRANSACTION_INITIATED = "UNWRAP_ETH_TRANSACTION_INITIATED"
    UNWRAP_ETH_TRANSACTION_CONFIRMING = "UNWRAP_ETH_TRANSACTION_CONFIRMING"
    UNWRAP_ETH_TRANSACTION_CONFIRMED = "UNWRAP_ETH_TRANSACTION_CONFIRMED"
    WBTC_BURN_CONFIRMED = "WBTC_BURN_CONFIRMED"
    UNWRAP_BTC_TRANSACTION_CREATING = "UNWRAP_BTC_TRANSACTION_CREATING"
    UNWRAP_BTC_TRANSACTION_BROADCASTED = "UNWRAP_BTC_TRANSACTION_BROADCASTED"
    UNWRAP_COMPLETED = "UNWRAP_COMPLETED"

    # Failure statuses
    FAILED_INSUFFICIENT_AMOUNT = "FAILED_INSUFFICIENT_AMOUNT"

class WrapTransaction(Base):
    __tablename__ = "wrap_transactions"
    id = Column(Integer, primary_key=True, index=True)
    btc_tx_id = Column(String, unique=True, index=True)
    wallet_id = Column(String, index=True)
    receiving_address = Column(String)
    amount = Column(Float)
    status = Column(String, default=TransactionStatus.WRAP_BTC_TRANSACTION_BROADCASTED)
    eth_tx_hash = Column(String)

class UnwrapTransaction(Base):
    __tablename__ = "unwrap_transactions"
    id = Column(Integer, primary_key=True, index=True)
    eth_tx_hash = Column(String, unique=True, index=True)
    wallet_id = Column(String, index=True, nullable=True)
    btc_receiving_address = Column(String, nullable=True)
    amount = Column(Float, nullable=True)
    status = Column(String, default=TransactionStatus.UNWRAP_ETH_TRANSACTION_INITIATED)
    btc_tx_id = Column(String, nullable=True)

Base.metadata.create_all(bind=engine)

class WrapRequest(BaseModel):
    signed_btc_tx: str

class UnwrapRequest(BaseModel):
    signed_eth_tx: str

@app.post("/initiate-wrap/")
async def initiate_wrap(wrap_request: WrapRequest):
    try:
        logger.info(f"received signed btc tx: {wrap_request.signed_btc_tx}")
        rpc_connection = AuthServiceProxy(btc_node_wallet_url)
        # Decode and extract information from the signed Bitcoin transaction
        decoded_tx = rpc_connection.decoderawtransaction(wrap_request.signed_btc_tx)
        logger.info(f"decoded tx: {decoded_tx}")
        op_return_data = next(output['scriptPubKey']['asm'] for output in decoded_tx['vout'] if output['scriptPubKey']['type'] == 'nulldata')
        logger.info(f"op return data: {op_return_data}")
        # remove the 'OP_RETURN ' prefix
        op_return_data = op_return_data.replace('OP_RETURN ', '')
        # reverse binascii.hexlify
        op_return_data = binascii.unhexlify(op_return_data).decode()
        logger.info(f"op return data: {op_return_data}")
        wallet_id, receiving_address = op_return_data.split(':')[1].split('-')
        logger.info(f"wallet id: {wallet_id}")
        logger.info(f"receiving address: {receiving_address}")
        amount = sum(output['value'] for output in decoded_tx['vout'] 
                     if output['scriptPubKey'].get('address') == bridge_btc_address)
        logger.info(f"Amount sent to bridge address: {amount} BTC")
        if amount < MIN_AMOUNT:
            raise Exception(f"Amount sent to bridge address is less than the minimum amount of {MIN_AMOUNT} BTC")
        
        # Broadcast the transaction
        btc_tx_id = rpc_connection.sendrawtransaction(wrap_request.signed_btc_tx)

        # Create a database record
        session = Session()
        new_wrap = WrapTransaction(
            btc_tx_id=btc_tx_id,
            wallet_id=wallet_id,
            receiving_address=receiving_address,
            amount=amount,
            status=TransactionStatus.WRAP_BTC_TRANSACTION_BROADCASTED
        )
        session.add(new_wrap)
        session.commit()
        session.close()

        return {"btc_tx_id": btc_tx_id, "status": TransactionStatus.WRAP_BTC_TRANSACTION_BROADCASTED}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/initiate-unwrap/")
async def initiate_unwrap(unwrap_request: UnwrapRequest):
    try:
        # Broadcast the transaction without decoding
        eth_tx_hash = w3.eth.send_raw_transaction(unwrap_request.signed_eth_tx)

        # Create a database record with minimal information
        session = Session()
        new_unwrap = UnwrapTransaction(
            eth_tx_hash=eth_tx_hash.hex(),
            status=TransactionStatus.UNWRAP_ETH_TRANSACTION_INITIATED
        )
        session.add(new_unwrap)
        session.commit()
        session.close()

        return {"eth_tx_hash": eth_tx_hash.hex(), "status": TransactionStatus.UNWRAP_ETH_TRANSACTION_INITIATED}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/wrap-status/{btc_tx_id}")
async def wrap_status(btc_tx_id: str):
    session = Session()
    wrap_tx = session.query(WrapTransaction).filter(WrapTransaction.btc_tx_id == btc_tx_id).first()
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

    return {"status": unwrap_tx.status, "btc_tx_id": unwrap_tx.btc_tx_id}

@app.get("/wrap-history/{wallet_id}")
async def wrap_history(wallet_id: str):
    session = Session()
    wrap_txs = session.query(WrapTransaction).filter(WrapTransaction.wallet_id == wallet_id).all()
    session.close()

    return [{"btc_tx_id": tx.btc_tx_id, "status": tx.status, "amount": tx.amount} for tx in wrap_txs]

@app.get("/unwrap-history/{wallet_id}")
async def unwrap_history(wallet_id: str):
    session = Session()
    unwrap_txs = session.query(UnwrapTransaction).filter(UnwrapTransaction.wallet_id == wallet_id).all()
    session.close()

    return [{"eth_tx_hash": tx.eth_tx_hash, "status": tx.status, "amount": tx.amount} for tx in unwrap_txs]

# Add these new endpoints
@app.get("/wrap-fee")
async def get_wrap_fee():
    return {
        "btc_fee": float(BTC_FEE),
        "eth_fee_in_wbtc": ETH_FEE_IN_WBTC
    }

@app.get("/unwrap-fee")
async def get_unwrap_fee():
    return {
        "btc_fee": float(BTC_FEE),
        "eth_gas_price": w3.eth.gas_price
    }

# Background tasks (to be run periodically)


async def process_wrap_transactions():
    max_retries = 3
    retry_delay = 5  # seconds
    rpc_connection = AuthServiceProxy(btc_node_wallet_url)
    for attempt in range(max_retries):
        try:
            session = Session()
            broadcasted_txs = session.query(WrapTransaction).filter(WrapTransaction.status == TransactionStatus.WRAP_BTC_TRANSACTION_BROADCASTED).all()

            for tx in broadcasted_txs:
                # Check Bitcoin transaction confirmation
                try:
                    btc_tx = rpc_connection.gettransaction(tx.btc_tx_id)
                    if btc_tx['confirmations'] >= 6:
                        # Convert BTC amount to satoshis, then to Wei
                        satoshis = int(tx.amount * 100000000)  # 1 BTC = 100,000,000 satoshis
                        
                        # Deduct the ETH fee in WBTC
                        satoshis -= ETH_FEE_IN_WBTC * 100000000

                        # Mint WBTC
                        nonce = w3.eth.get_transaction_count(os.getenv("OWNER_ADDRESS"))
                        chain_id = w3.eth.chain_id  # Get the current chain ID
                        mint_tx = compiled_sol.functions.mint(tx.receiving_address, satoshis).build_transaction({
                            'chainId': chain_id,
                            'gas': 2000000,
                            'gasPrice': w3.eth.gas_price,
                            'nonce': nonce,
                        })
                        signed_tx = w3.eth.account.sign_transaction(mint_tx, os.getenv("OWNER_PRIVATE_KEY"))
                        eth_tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

                        tx.status = TransactionStatus.WBTC_MINTING_IN_PROGRESS
                        tx.eth_tx_hash = eth_tx_hash.hex()
                except JSONRPCException as e:
                    logger.error(f"JSONRPC error for transaction {tx.btc_tx_id}: {e}")
                    continue

            session.commit()

            minting_txs = session.query(WrapTransaction).filter(WrapTransaction.status == TransactionStatus.WBTC_MINTING_IN_PROGRESS).all()

            for tx in minting_txs:
                # Check WBTC minting transaction confirmation
                eth_tx = w3.eth.get_transaction_receipt(tx.eth_tx_hash)
                if eth_tx and eth_tx['status'] == 1:
                    tx.status = TransactionStatus.WRAP_COMPLETED

            session.commit()
            session.close()
            break  # If we get here, the function completed successfully

        except (BrokenPipeError, ConnectionError) as e:
            logger.error(f"Connection error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error("Max retries reached. Please check your Bitcoin node connection.")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            break
        finally:
            if 'session' in locals():
                session.close()

async def process_unwrap_transactions():
    session = Session()
    initiated_txs = session.query(UnwrapTransaction).filter(UnwrapTransaction.status == TransactionStatus.UNWRAP_ETH_TRANSACTION_INITIATED).all()
    rpc_connection = AuthServiceProxy(btc_node_wallet_url)
    
    for tx in initiated_txs:
        try:
            eth_tx_receipt = w3.eth.get_transaction_receipt(tx.eth_tx_hash)
            if eth_tx_receipt and eth_tx_receipt['status'] == 1:
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
                    amount = burnt_amount/100000000
                    # check if amount is less than MIN_AMOUNT or amount is not a number
                    if amount < MIN_AMOUNT or math.isnan(amount):
                        tx.status = TransactionStatus.FAILED_INSUFFICIENT_AMOUNT
                        logger.error(f"Amount sent to bridge address is less than the minimum amount of {MIN_AMOUNT} BTC, eth_tx_hash: {tx.eth_tx_hash}")
                        continue

                    # Extract wallet_id and btc_receiving_address from the _data parameter
                    data_param = decoded_input[1]['data'].decode('utf-8')
                    wallet_id, btc_receiving_address = data_param.split(':')[1].split('-')

                    # Update the database record with extracted information
                    tx.wallet_id = wallet_id
                    tx.btc_receiving_address = btc_receiving_address
                    tx.amount = amount
                    tx.status = TransactionStatus.UNWRAP_ETH_TRANSACTION_CONFIRMING
                else:
                    logger.error(f"Unexpected function call: {func_signature}")
                    continue

        except Exception as e:
            logger.error(f"Error processing transaction {tx.eth_tx_hash}: {e}")
            continue

    session.commit()

    confirming_txs = session.query(UnwrapTransaction).filter(UnwrapTransaction.status == TransactionStatus.UNWRAP_ETH_TRANSACTION_CONFIRMING).all()

    for tx in confirming_txs:
        try:
            eth_tx_receipt = w3.eth.get_transaction_receipt(tx.eth_tx_hash)
            # Check the number of confirmations
            block_number = eth_tx_receipt['blockNumber']
            current_block = w3.eth.block_number
            confirmations = current_block - block_number
            if confirmations >= 6:
                tx.status = TransactionStatus.UNWRAP_ETH_TRANSACTION_CONFIRMED
            else:
                logger.info(f"Transaction {tx.eth_tx_hash} not yet confirmed")
        except Exception as e:
            logger.error(f"Error processing transaction {tx.eth_tx_hash}: {e}")
            continue

    session.commit()

    confirmed_txs = session.query(UnwrapTransaction).filter(UnwrapTransaction.status == TransactionStatus.UNWRAP_ETH_TRANSACTION_CONFIRMED).all()

    for tx in confirmed_txs:
        try:
            # Fetch unspent transactions to use as inputs
                
            bridge_address = os.getenv('BRIDGE_BTC_ADDRESS')
            amount_to_send = Decimal(str(tx.amount)) - BTC_FEE
            total_needed = amount_to_send + BTC_FEE

            # Fetch unspent UTXOs with minimum amount and sum
            unspent = rpc_connection.listunspent(0, 9999999, [bridge_address], False, {
                "minimumAmount": "0.00000546",
                "minimumSumAmount": float(total_needed)
            })
            
            if not unspent:
                logger.error("No unspent transactions found")
                raise Exception("No unspent transactions found")

            # Calculate total available amount
            total_amount = sum(Decimal(str(input['amount'])) for input in unspent)

            if total_amount < total_needed:
                logger.error("Not enough funds to create the transaction")
                raise Exception("Not enough funds to create the transaction")

            # Prepare inputs and outputs
            tx_inputs = [{"txid": input['txid'], "vout": input['vout']} for input in unspent]
            tx_outputs = {
                tx.btc_receiving_address: float(amount_to_send),
                bridge_address: float(total_amount - total_needed)  # Change
            }

            logger.info(f"tx_outputs: {tx_outputs}")
            # Format OP_RETURN data as hexadecimal
            op_return_data = f"wrp:{tx.wallet_id}-{os.getenv('WBTC_RECEIVE_ADDRESS')}"
            op_return_hex = binascii.hexlify(op_return_data.encode()).decode()
            tx_outputs["data"] = op_return_hex

            # Create raw transaction
            btc_tx = rpc_connection.createrawtransaction(tx_inputs, tx_outputs)

            signed_tx = rpc_connection.signrawtransactionwithwallet(btc_tx)
            btc_tx_id = rpc_connection.sendrawtransaction(signed_tx['hex'])

            tx.status = TransactionStatus.UNWRAP_BTC_TRANSACTION_BROADCASTED
            tx.btc_tx_id = btc_tx_id

        except Exception as e:
            logger.error(f"Error creating Bitcoin transaction for {tx.eth_tx_hash}: {e}")

    session.commit()

    broadcasted_txs = session.query(UnwrapTransaction).filter(UnwrapTransaction.status == TransactionStatus.UNWRAP_BTC_TRANSACTION_BROADCASTED).all()

    for tx in broadcasted_txs:
        try:
            btc_tx = rpc_connection.gettransaction(tx.btc_tx_id)
            if btc_tx['confirmations'] >= 6:
                tx.status = TransactionStatus.UNWRAP_COMPLETED
        except Exception as e:
            logger.error(f"Error checking Bitcoin transaction {tx.btc_tx_id}: {e}")

    session.commit()
    session.close()

# Add jobs to the scheduler
scheduler.add_job(process_wrap_transactions, IntervalTrigger(minutes=2))
scheduler.add_job(process_unwrap_transactions, IntervalTrigger(minutes=2))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)