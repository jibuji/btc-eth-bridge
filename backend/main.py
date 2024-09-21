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
import time
from bitcoinrpc.authproxy import JSONRPCException

load_dotenv()

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
    print(f"Failed to load wallet: {e}")

# Instead of creating a new AuthServiceProxy, update the existing one
btc_node_wallet_url = f"{btc_node_url}/wallet/{btc_wallet_name}"



# Database setup
engine = create_engine(os.getenv("DATABASE_URL"))
Base = declarative_base()
Session = sessionmaker(bind=engine)

class WrapTransaction(Base):
    __tablename__ = "wrap_transactions"
    id = Column(Integer, primary_key=True, index=True)
    btc_tx_id = Column(String, unique=True, index=True)
    wallet_id = Column(String, index=True)
    receiving_address = Column(String)
    amount = Column(Float)
    status = Column(String)
    eth_tx_hash = Column(String)

class UnwrapTransaction(Base):
    __tablename__ = "unwrap_transactions"
    id = Column(Integer, primary_key=True, index=True)
    eth_tx_hash = Column(String, unique=True, index=True)
    wallet_id = Column(String, index=True)
    btc_receiving_address = Column(String)
    amount = Column(Float)
    status = Column(String)
    btc_tx_id = Column(String)

Base.metadata.create_all(bind=engine)

class WrapRequest(BaseModel):
    signed_btc_tx: str

class UnwrapRequest(BaseModel):
    signed_eth_tx: str

@app.post("/initiate-wrap/")
async def initiate_wrap(wrap_request: WrapRequest):
    try:
        print("received signed btc tx:", wrap_request.signed_btc_tx)
        rpc_connection = AuthServiceProxy(btc_node_wallet_url)
        # Decode and extract information from the signed Bitcoin transaction
        decoded_tx = rpc_connection.decoderawtransaction(wrap_request.signed_btc_tx)
        print("decoded tx:", decoded_tx)
        op_return_data = next(output['scriptPubKey']['asm'] for output in decoded_tx['vout'] if output['scriptPubKey']['type'] == 'nulldata')
        print("op return data:", op_return_data)
        # remove the 'OP_RETURN ' prefix
        op_return_data = op_return_data.replace('OP_RETURN ', '')
        # reverse binascii.hexlify
        op_return_data = binascii.unhexlify(op_return_data).decode()
        print("op return data:", op_return_data)
        wallet_id, receiving_address = op_return_data.split(':')[1].split('-')
        print("wallet id:", wallet_id)
        print("receiving address:", receiving_address)
        amount = sum(output['value'] for output in decoded_tx['vout'] if output['scriptPubKey']['type'] != 'nulldata')

        # Broadcast the transaction
        btc_tx_id = rpc_connection.sendrawtransaction(wrap_request.signed_btc_tx)

        # Create a database record
        session = Session()
        new_wrap = WrapTransaction(
            btc_tx_id=btc_tx_id,
            wallet_id=wallet_id,
            receiving_address=receiving_address,
            amount=amount,
            status="BROADCASTED"
        )
        session.add(new_wrap)
        session.commit()
        session.close()

        return {"btc_tx_id": btc_tx_id, "status": "BROADCASTED"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/initiate-unwrap/")
async def initiate_unwrap(unwrap_request: UnwrapRequest):
    try:
        # Decode and extract information from the signed Ethereum transaction
        decoded_tx = w3.eth.account.decode_transaction(unwrap_request.signed_eth_tx)
        calldata = decoded_tx['data']
        wallet_id, btc_receiving_address = calldata.split(':')[1].split('-')
        amount = decoded_tx['value']

        # Broadcast the transaction
        eth_tx_hash = w3.eth.send_raw_transaction(unwrap_request.signed_eth_tx)

        # Create a database record
        session = Session()
        new_unwrap = UnwrapTransaction(
            eth_tx_hash=eth_tx_hash.hex(),
            wallet_id=wallet_id,
            btc_receiving_address=btc_receiving_address,
            amount=amount,
            status="INITIATED"
        )
        session.add(new_unwrap)
        session.commit()
        session.close()

        return {"eth_tx_hash": eth_tx_hash.hex(), "status": "INITIATED"}
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

# Background tasks (to be run periodically)


async def process_wrap_transactions():
    max_retries = 3
    retry_delay = 5  # seconds
    rpc_connection = AuthServiceProxy(btc_node_wallet_url)
    for attempt in range(max_retries):
        try:
            session = Session()
            broadcasted_txs = session.query(WrapTransaction).filter(WrapTransaction.status == "BROADCASTED").all()

            for tx in broadcasted_txs:
                # Check Bitcoin transaction confirmation
                try:
                    btc_tx = rpc_connection.gettransaction(tx.btc_tx_id)
                    if btc_tx['confirmations'] >= 6:
                        # Convert BTC amount to satoshis, then to Wei
                        satoshis = int(tx.amount * 100000000)  # 1 BTC = 100,000,000 satoshis
                        wei_amount = Web3.to_wei(satoshis, 'wei')  # 1 WBTC = 1e8 wei (same as 1 satoshi)

                        # Mint WBTC
                        nonce = w3.eth.get_transaction_count(os.getenv("OWNER_ADDRESS"))
                        chain_id = w3.eth.chain_id  # Get the current chain ID
                        mint_tx = compiled_sol.functions.mint(tx.receiving_address, wei_amount).build_transaction({
                            'chainId': chain_id,
                            'gas': 2000000,
                            'gasPrice': w3.eth.gas_price,
                            'nonce': nonce,
                        })
                        signed_tx = w3.eth.account.sign_transaction(mint_tx, os.getenv("OWNER_PRIVATE_KEY"))
                        eth_tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

                        tx.status = "MINTING"
                        tx.eth_tx_hash = eth_tx_hash.hex()
                except JSONRPCException as e:
                    print(f"JSONRPC error for transaction {tx.btc_tx_id}: {e}")
                    continue

            session.commit()

            minting_txs = session.query(WrapTransaction).filter(WrapTransaction.status == "MINTING").all()

            for tx in minting_txs:
                # Check WBTC minting transaction confirmation
                eth_tx = w3.eth.get_transaction_receipt(tx.eth_tx_hash)
                if eth_tx and eth_tx['status'] == 1:
                    tx.status = "COMPLETED"

            session.commit()
            session.close()
            break  # If we get here, the function completed successfully

        except (BrokenPipeError, ConnectionError) as e:
            print(f"Connection error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                print("Max retries reached. Please check your Bitcoin node connection.")
        except Exception as e:
            print(f"Unexpected error: {e}")
            break
        finally:
            if 'session' in locals():
                session.close()

async def process_unwrap_transactions():
    session = Session()
    initiated_txs = session.query(UnwrapTransaction).filter(UnwrapTransaction.status == "INITIATED").all()
    rpc_connection = AuthServiceProxy(btc_node_wallet_url)
    for tx in initiated_txs:
        # Check Ethereum transaction confirmation
        eth_tx = w3.eth.get_transaction_receipt(tx.eth_tx_hash)
        if eth_tx and eth_tx['status'] == 1:
            # Generate and broadcast Bitcoin transaction
            btc_tx = rpc_connection.createrawtransaction(
                [],
                {tx.btc_receiving_address: tx.amount, "data": f"wrp:{tx.wallet_id}-{tx.eth_tx_hash}"}
            )
            signed_tx = rpc_connection.signrawtransactionwithwallet(btc_tx)
            btc_tx_id = rpc_connection.sendrawtransaction(signed_tx['hex'])

            tx.status = "BROADCASTED"
            tx.btc_tx_id = btc_tx_id

    session.commit()

    broadcasted_txs = session.query(UnwrapTransaction).filter(UnwrapTransaction.status == "BROADCASTED").all()

    for tx in broadcasted_txs:
        # Check Bitcoin transaction confirmation
        btc_tx = rpc_connection.gettransaction(tx.btc_tx_id)
        if btc_tx['confirmations'] >= 6:
            tx.status = "COMPLETED"

    session.commit()
    session.close()

# Add jobs to the scheduler
scheduler.add_job(process_wrap_transactions, IntervalTrigger(minutes=2))
scheduler.add_job(process_unwrap_transactions, IntervalTrigger(minutes=2))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)