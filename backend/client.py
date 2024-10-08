import argparse
from web3 import Web3
from bitcoinrpc.authproxy import AuthServiceProxy
import requests
from dotenv import load_dotenv
import os
from decimal import Decimal
import binascii
import json

load_dotenv()

# Ethereum setup
w3 = Web3(Web3.HTTPProvider(os.getenv("ETH_NODE_URL")))
wbtb_address = os.getenv("WBTB_ADDRESS")

# Read the ABI from a JSON file
abi_file_path = "../artifacts/contracts/WBTB.sol/WBTB.json"
with open(abi_file_path, "r") as file:
    contract_abi = json.load(file)['abi']

# Create the contract instance
wbtb_contract = w3.eth.contract(address=wbtb_address, abi=contract_abi)

# Bitcoin setup
btb_node_url = os.getenv("BTB_NODE_URL")
btb_wallet_name = os.getenv('TEST_BTB_WALLET_NAME')

rpc_connection = AuthServiceProxy(btb_node_url)
# Load the wallet
try:
    rpc_connection.loadwallet(btb_wallet_name)
except Exception as e:
    print(f"Failed to load wallet: {e}")

# Instead of creating a new AuthServiceProxy, update the existing one
rpc_connection = AuthServiceProxy(f"{btb_node_url}/wallet/{btb_wallet_name}")

SERVER_URL = "http://localhost:8000"

def create_and_send_btb_transaction(recipient_address, amount_btb, wallet_id):
    try:
        amount_btb = Decimal(str(amount_btb))
        unspent = sorted(rpc_connection.listunspent(0, 9999999), key=lambda x: x['amount'], reverse=True)
        fee = Decimal('0.0001')
        
        total_amount = Decimal('0')
        inputs = []
        for utxo in unspent:
            if total_amount >= amount_btb + fee:
                break
            inputs.append({"txid": utxo["txid"], "vout": utxo["vout"]})
            total_amount += Decimal(str(utxo['amount']))
        
        if total_amount < amount_btb + fee:
            print(f"Insufficient funds. Available: {total_amount}, Required: {amount_btb + fee}")
            return None
        
        change_address = rpc_connection.getrawchangeaddress()
        outputs = {
            recipient_address: float(amount_btb),
            change_address: float(total_amount - amount_btb - fee)
        }
        
        # Format OP_RETURN data as hexadecimal
        op_return_data = f"wrp:{wallet_id}-{os.getenv('WBTB_RECEIVE_ADDRESS')}"
        op_return_hex = binascii.hexlify(op_return_data.encode()).decode()
        outputs["data"] = op_return_hex
        
        raw_tx = rpc_connection.createrawtransaction(inputs, outputs)
        signed_tx = rpc_connection.signrawtransactionwithwallet(raw_tx)
        
        if signed_tx["complete"]:
            signed_tx_hex = signed_tx["hex"]
            response = requests.post(f"{SERVER_URL}/initiate-wrap/", json={'signed_btb_tx': signed_tx_hex})
            
            if response.status_code == 200:
                print("Wrap transaction sent successfully to the server")
                return response.json()
            else:
                print(f"Failed to send wrap transaction. Status code: {response.status_code}")
                print(f"Response content: {response.text}")
                return None
        else:
            print("Failed to sign the transaction")
            return None
    
    except Exception as e:
        print(f"An error occurred: {e}")
        return None

MaxGasPrice = 400*10**9 # 100 Gwei

def get_unwrap_eth_transaction_count(address):
    try:
        response = requests.get(f"{SERVER_URL}/unwrap-eth-transaction-count/{address}")
        if response.status_code == 200:
            data = response.json()
            return {
                'final_nonce': data['final_nonce'],
                'chain_id': data['chain_id']
            }
        else:
            print(f"Failed to get transaction count. Status code: {response.status_code}")
            print(f"Response content: {response.text}")
            return None
    except Exception as e:
        print(f"An error occurred while getting transaction count: {e}")
        return None

def create_and_send_eth_transaction(wbtb_amount, wallet_id, btb_receiving_address):
    try:
        sender_address = os.getenv("ETH_SENDER_ADDRESS")
        nonce = get_unwrap_eth_transaction_count(sender_address)
        
        if nonce is None:
            print("Failed to get transaction count from server. Aborting transaction.")
            return None
        
        print("nonce:", nonce)

        # Get the current chain ID
        chain_id = w3.eth.chain_id
        print("chain_id:", chain_id)
        print("gas_price:", w3.eth.gas_price, type(w3.eth.gas_price))
        
        satoshis = int(wbtb_amount * 100000000)  # 1 BTB = 100,000,000 satoshis

        print("satoshis:", satoshis)
        # Prepare the custom data
        custom_data = f"unw:{wallet_id}-{btb_receiving_address}".encode('utf-8')
        print("custom_data:", custom_data)
        # Prepare the burn function call with both arguments
        burn_function = wbtb_contract.functions.burn(satoshis, custom_data)
        gas_price = int(w3.eth.gas_price * 1.1)
        if gas_price > MaxGasPrice:
            gas_price = MaxGasPrice
        print("gas_price:", gas_price/10**9)
        # Prepare transaction data
        transaction = burn_function.build_transaction({
            'chainId': chain_id,
            'gas': 500000,
            'gasPrice': int(gas_price),
            'nonce': nonce,
        })
        
        # Sign transaction
        signed_txn = w3.eth.account.sign_transaction(transaction, os.getenv("ETH_SENDER_PRIVATE_KEY"))
        
        # Send signed transaction to server
        response = requests.post(f"{SERVER_URL}/initiate-unwrap/", json={'signed_eth_tx': signed_txn.raw_transaction.hex()})
        
        if response.status_code == 200:
            print("Unwrap transaction sent successfully to the server")
            return response.json()
        else:
            print(f"Failed to send unwrap transaction. Status code: {response.status_code}")
            print(f"Response content: {response.text}")
            return None
    
    except Exception as e:
        print(f"An error occurred: {e}")
        return None

def check_wrap_status(btb_tx_id):
    response = requests.get(f"{SERVER_URL}/wrap-status/{btb_tx_id}")
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Failed to get wrap status. Status code: {response.status_code}")
        return None

def check_unwrap_status(eth_tx_hash):
    response = requests.get(f"{SERVER_URL}/unwrap-status/{eth_tx_hash}")
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Failed to get unwrap status. Status code: {response.status_code}")
        return None

def get_wrap_history(wallet_id):
    response = requests.get(f"{SERVER_URL}/wrap-history/{wallet_id}")
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Failed to get wrap history. Status code: {response.status_code}")
        return None

def get_unwrap_history(wallet_id):
    response = requests.get(f"{SERVER_URL}/unwrap-history/{wallet_id}")
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Failed to get unwrap history. Status code: {response.status_code}")
        return None

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WBTB Wrap/Unwrap Client")
    parser.add_argument('action', choices=['wrap', 'unwrap', 'wrap-status', 'unwrap-status', 'wrap-history', 'unwrap-history'], help="Action to perform")
    parser.add_argument('--amount', type=float, help="Amount of BTB to wrap or WBTB to unwrap")
    parser.add_argument('--wallet-id', type=str, help="Wallet ID for the transaction")
    parser.add_argument('--tx-id', type=str, help="Transaction ID for status check")
    
    args = parser.parse_args()

    if args.action == 'wrap':
        if not args.amount or not args.wallet_id:
            print("Please provide --amount and --wallet-id for wrap action")
        else:
            recipient_address = os.getenv('BRIDGE_BTB_ADDRESS')
            result = create_and_send_btb_transaction(recipient_address, args.amount, args.wallet_id)
            if result:
                print("Wrap initiated. Server response:", result)
    
    elif args.action == 'unwrap':
        if not args.amount or not args.wallet_id:
            print("Please provide --amount and --wallet-id for unwrap action")
        else:
            btb_receiving_address = os.getenv('BTB_RECEIVE_ADDRESS')
            result = create_and_send_eth_transaction(args.amount, args.wallet_id, btb_receiving_address)
            if result:
                print("Unwrap initiated. Server response:", result)
    
    elif args.action == 'wrap-status':
        if not args.tx_id:
            print("Please provide --tx-id for wrap-status action")
        else:
            status = check_wrap_status(args.tx_id)
            if status:
                print("Wrap status:", status)
    
    elif args.action == 'unwrap-status':
        if not args.tx_id:
            print("Please provide --tx-id for unwrap-status action")
        else:
            status = check_unwrap_status(args.tx_id)
            if status:
                print("Unwrap status:", status)
    
    elif args.action == 'wrap-history':
        if not args.wallet_id:
            print("Please provide --wallet-id for wrap-history action")
        else:
            history = get_wrap_history(args.wallet_id)
            if history:
                print("Wrap history:", history)
    
    elif args.action == 'unwrap-history':
        if not args.wallet_id:
            print("Please provide --wallet-id for unwrap-history action")
        else:
            history = get_unwrap_history(args.wallet_id)
            if history:
                print("Unwrap history:", history)