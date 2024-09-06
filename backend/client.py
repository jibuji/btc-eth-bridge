from bitcoinrpc.authproxy import AuthServiceProxy, JSONRPCException
import requests
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

def create_and_send_btc_transaction(sender_address, recipient_address, amount_btc, server_url, btc_node_url, wallet_name):
    # Connect to the Bitcoin Core RPC
    wallet_url = f"{btc_node_url}/wallet/{wallet_name}"
    print(f"wallet_url: {wallet_url}")
    rpc_connection = AuthServiceProxy(wallet_url)

    try:
        # Create a raw transaction
        inputs = [{"txid": unspent["txid"], "vout": unspent["vout"]} for unspent in rpc_connection.listunspent(0, 9999999, [], False, {"minimumAmount": 1, "maximumAmount": 1000})]
        outputs = {recipient_address: amount_btc}
        raw_tx = rpc_connection.createrawtransaction(inputs, outputs)

        # Sign the raw transaction
        signed_tx = rpc_connection.signrawtransactionwithwallet(raw_tx)

        if signed_tx["complete"]:
            # Get the raw signed transaction
            signed_tx_hex = signed_tx["hex"]

            # Send the signed transaction to the server
            response = requests.post(server_url, json={'signed_transaction': signed_tx_hex})

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
    sender_address = os.getenv('BTC_SENDER_ADDRESS')
    recipient_address = os.getenv('BRIDGE_BTC_ADDRESS')
    amount_to_send = 0.001  # BTC
    server_url = "http://0.0.0.0:8000/initiate-wrap"
    btc_node_url = os.getenv('BTC_NODE_URL')
    print(f"sender_address: {sender_address}")
    print(f"recipient_address: {recipient_address}")
    print(f"amount_to_send: {amount_to_send}")
    print(f"server_url: {server_url}")
    print(f"btc_node_url: {btc_node_url}")
    print(f"wallet_name: {wallet_name}")
    result = create_and_send_btc_transaction(sender_address, recipient_address, amount_to_send, server_url, btc_node_url, wallet_name)
    if result:
        print("Server response:", result)
