from bitcoinrpc.authproxy import AuthServiceProxy, JSONRPCException
import requests
from dotenv import load_dotenv
import os
from decimal import Decimal

# Load environment variables from .env file
load_dotenv()

btc_rpc = None
BTC_WALLET_NAME = "default"

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

def create_and_send_btc_transaction(recipient_address, amount_btc, server_url, ethereum_address):
    try:
        # Convert amount_btc to Decimal
        amount_btc = Decimal(str(amount_btc))
        
        # Get unspent outputs and sort them by amount in descending order
        unspent = sorted(btc_rpc.listunspent(0, 9999999), key=lambda x: x['amount'], reverse=True)
        
        # Calculate fee (you may need to adjust this based on your network's current fee rate)
        fee = Decimal('0.0001')  # Set a reasonable fee, adjust as needed
        
        total_amount = Decimal('0')
        inputs = []
        for utxo in unspent:
            if total_amount >= amount_btc + fee:
                break
            inputs.append({"txid": utxo["txid"], "vout": utxo["vout"]})
            total_amount += Decimal(str(utxo['amount']))
        
        if total_amount < amount_btc + fee:
            print(f"Insufficient funds. Available: {total_amount}, Required: {amount_btc + fee}")
            return None
        
        print(f"total_amount: {total_amount}")
        print(f"amount_btc: {amount_btc}")
        print(f"fee: {fee}")
        print(f"inputs: {inputs}")
        # Create outputs
        outputs = {
            recipient_address: float(amount_btc),  # Convert back to float for Bitcoin Core
            btc_rpc.getrawchangeaddress(): float(total_amount - amount_btc - fee)  # Convert back to float
        }
        print(f"outputs: {outputs}")
        
        # Create raw transaction
        raw_tx = btc_rpc.createrawtransaction(inputs, outputs)
        
        # Sign the raw transaction
        signed_tx = btc_rpc.signrawtransactionwithwallet(raw_tx)
        
        if signed_tx["complete"]:
            # Get the raw signed transaction
            signed_tx_hex = signed_tx["hex"]
            print(f"signed_tx_hex: {signed_tx_hex}")
            print(f"ethereum_address: {ethereum_address}")  
            # Send the signed transaction to the server
            response = requests.post(server_url, json={'signed_btc_tx': signed_tx_hex, 'ethereum_address': ethereum_address})
            
            if response.status_code == 200:
                print("Transaction sent successfully to the server")
                return response.json()
            else:
                print(f"Failed to send transaction. Status code: {response.status_code}")
                print(f"Response content: {response.text}")
                return None
        else:
            print("Failed to sign the transaction")
            return None
    
    except JSONRPCException as e:
        print(f"An error occurred with the Bitcoin RPC: {e}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"An error occurred while sending the request to the server: {e}")
        return None

# Example usage
if __name__ == "__main__":
    wallet_name = "default"
    recipient_address = os.getenv('BRIDGE_BTC_ADDRESS')
    ethereum_address = os.getenv('WBTC_RECEIVE_ADDRESS')
    amount_to_send = 0.001  # BTC
    server_url = "http://localhost:8000/initiate-wrap"
    btc_node_url = os.getenv('BTC_NODE_URL')
    print(f"recipient_address: {recipient_address}")
    print(f"amount_to_send: {amount_to_send}")
    print(f"server_url: {server_url}")
    print(f"btc_node_url: {btc_node_url}")
    print(f"wallet_name: {wallet_name}")
    result = create_and_send_btc_transaction(recipient_address, amount_to_send, server_url, ethereum_address)
    if result:
        print("Server response:", result)
