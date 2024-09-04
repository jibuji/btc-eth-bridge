from fastapi import FastAPI, HTTPException
from web3 import Web3
from pydantic import BaseModel
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Connect to Ethereum node (local Hardhat node)
w3 = Web3(Web3.HTTPProvider('http://localhost:8545'))

# WBTC contract address and ABI
WBTC_ADDRESS = os.getenv('WBTC_ADDRESS')
WBTC_ABI = [
    {"inputs": [{"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}], "name": "mint", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "from", "type": "address"}, {"internalType": "uint256", "name": "amount", "type": "uint256"}], "name": "burn", "outputs": [], "stateMutability": "nonpayable", "type": "function"}
]

wbtc_contract = w3.eth.contract(address=WBTC_ADDRESS, abi=WBTC_ABI)

class WrapRequest(BaseModel):
    bitcoin_address: str
    ethereum_address: str
    amount: float

class UnwrapRequest(BaseModel):
    ethereum_address: str
    bitcoin_address: str
    amount: float

@app.post("/wrap")
async def wrap(request: WrapRequest):
    try:
        # Convert BTC amount to Wei (1 BTC = 10^8 satoshis = 10^18 Wei)
        amount_wei = int(request.amount * 10**10)
        
        # Call the mint function on the WBTC contract
        tx = wbtc_contract.functions.mint(
            request.ethereum_address,
            amount_wei
        ).build_transaction({
            'from': os.getenv('OWNER_ADDRESS'),
            'nonce': w3.eth.get_transaction_count(os.getenv('OWNER_ADDRESS')),
        })
        
        # Sign and send the transaction
        signed_tx = w3.eth.account.sign_transaction(tx, os.getenv('OWNER_PRIVATE_KEY'))
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        
        return {"message": "WBTC minted successfully", "transaction_hash": tx_hash.hex()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/unwrap")
async def unwrap(request: UnwrapRequest):
    try:
        # Convert BTC amount to Wei
        amount_wei = int(request.amount * 10**10)
        
        # Call the burn function on the WBTC contract
        tx = wbtc_contract.functions.burn(
            request.ethereum_address,
            amount_wei
        ).build_transaction({
            'from': os.getenv('OWNER_ADDRESS'),
            'nonce': w3.eth.get_transaction_count(os.getenv('OWNER_ADDRESS')),
        })
        
        # Sign and send the transaction
        signed_tx = w3.eth.account.sign_transaction(tx, os.getenv('OWNER_PRIVATE_KEY'))
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        
        return {"message": "WBTC burned successfully", "transaction_hash": tx_hash.hex()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)