from fastapi import FastAPI, HTTPException, BackgroundTasks
from web3 import Web3
from pydantic import BaseModel
import os
from dotenv import load_dotenv
import bitcoinrpc.authproxy
from bitcoinrpc.authproxy import AuthServiceProxy
from decimal import Decimal

load_dotenv()

app = FastAPI()

# Connect to Ethereum node
w3 = Web3(Web3.HTTPProvider(os.getenv('ETH_NODE_URL')))

# Connect to Bitcoin node
btc_rpc = AuthServiceProxy(os.getenv('BTC_NODE_URL'))
BRIDGE_BTC_ADDRESS = os.getenv('BRIDGE_BTC_ADDRESS')

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
            if vout['scriptPubKey']['addresses'][0] == BRIDGE_BTC_ADDRESS:
                return vout['value']  # Return the amount sent to the bridge
        raise ValueError("Transaction does not have output to bridge's BTC address")
    except Exception as e:
        raise ValueError(f"Failed to verify BTC transaction: {str(e)}")

@app.post("/initiate-wrap")
async def initiate_wrap(request: WrapRequest, background_tasks: BackgroundTasks):
    try:
        # Verify the Bitcoin transaction
        btc_amount = verify_btc_transaction(request.btc_tx_id, BRIDGE_BTC_ADDRESS)
        
        # Convert BTC amount to Wei (1 BTC = 10^8 satoshis = 10^8 Wei for WBTC)
        amount_wei = int(btc_amount * 10**8)
        
        # Add minting task to background tasks
        background_tasks.add_task(mint_wbtc, request.ethereum_address, amount_wei)
        
        return {"message": "Wrap initiated, WBTC will be minted after confirmation"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

def mint_wbtc(ethereum_address: str, amount_wei: int):
    try:
        tx = wbtc_contract.functions.mint(
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
        
        # Log the successful minting
        print(f"WBTC minted successfully. Transaction hash: {tx_hash.hex()}")
    except Exception as e:
        print(f"Failed to mint WBTC: {str(e)}")

@app.post("/unwrap")
async def unwrap(request: UnwrapRequest):
    try:
        # Convert BTC amount to Wei (1 BTC = 10^8 satoshis = 10^8 Wei for WBTC)
        amount_wei = int(request.amount * 10**8)
        
        # Call the burn function on the WBTC contract
        tx = wbtc_contract.functions.burn(
            request.ethereum_address,
            amount_wei
        ).build_transaction({
            'from': os.getenv('OWNER_ADDRESS'),
            'nonce': w3.eth.get_transaction_count(os.getenv('OWNER_ADDRESS')),
        })
        
        signed_tx = w3.eth.account.sign_transaction(tx, os.getenv('OWNER_PRIVATE_KEY'))
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        # Wait for transaction confirmation
        w3.eth.wait_for_transaction_receipt(tx_hash)
        
        # Initiate BTC transfer (this should be done securely, possibly with multi-sig)
        btc_tx = btc_rpc.sendtoaddress(request.bitcoin_address, request.amount)
        
        return {"message": "Unwrap completed", "eth_tx_hash": tx_hash.hex(), "btc_tx_id": btc_tx}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/initiate-unwrap")
async def initiate_unwrap(request: UnwrapRequest):
    try:
        # Convert BTC amount to Wei
        amount_wei = int(request.amount * 10**8)
        
        # Check if the bridge has enough BTC balance
        unspent = btc_rpc.listunspent(0, 9999999, [BRIDGE_BTC_ADDRESS])
        total_balance = sum(Decimal(u['amount']) for u in unspent)
        if total_balance < Decimal(str(request.amount)):
            raise HTTPException(status_code=400, detail="Insufficient BTC balance in the bridge")
        
        # Burn WBTC first
        burn_tx_hash = burn_wbtc(request.ethereum_address, amount_wei)
        
        # Create a raw transaction
        inputs = [{"txid": u['txid'], "vout": u['vout']} for u in unspent]
        outputs = {
            request.bitcoin_address: request.amount,
            BRIDGE_BTC_ADDRESS: float(total_balance - Decimal(str(request.amount)))  # Change address
        }
        raw_tx = btc_rpc.createrawtransaction(inputs, outputs)
        
        # At this point, you would typically sign the transaction offline
        # For demonstration, we'll use the signrawtransactionwithwallet method
        # WARNING: This assumes the wallet is unlocked on the node, which is not recommended for production
        signed_tx = btc_rpc.signrawtransactionwithwallet(raw_tx)
        
        if not signed_tx['complete']:
            raise Exception("Failed to sign the transaction")
        
        # Broadcast the signed transaction
        btc_tx_id = btc_rpc.sendrawtransaction(signed_tx['hex'])
        
        return {"message": "Unwrap initiated", "eth_burn_tx_hash": burn_tx_hash.hex(), "btc_tx_id": btc_tx_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)