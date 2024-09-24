

## Wallet ID Generation

A wallet ID is a unique identifier for a Bitcoin wallet, it is a base58 encoded string of `Bridge-Delegator-ETH-Addr`, which is implemented in frontend.

## Wrap Logic (BTC to WBTC)

The wrapping process converts BTC to WBTC through the following steps:

1. Initiation:
   - Endpoint: `/initiate-wrap/`
   - Input: Signed Bitcoin transaction
   - Transaction contains: amount, receiving Ethereum address, wallet ID
   - Data format in OP_RETURN: `wrp:wallet_id-receiving_address`

2. Processing:
   a. Extract `wallet_id`, `receiving_address` from the signed Bitcoin transaction
   b. Broadcast the signed Bitcoin transaction
   c. If successful:
      - Create a database record with status `BROADCASTED`
   d. If failed: Return an error

3. Response:
   - Return Bitcoin transaction ID
   - Indicate wrap initiation and transaction broadcast

4. Background Processing:
   - For `BROADCASTED` status:
     - Check Bitcoin transaction confirmation
     - If confirmed: Mint WBTC, update status to `MINTING`, store minting tx hash
   - For `MINTING` status:
     - Check WBTC minting transaction confirmation
     - If confirmed: Update status to `COMPLETED`

5. Status Checking:
   - Endpoint: `/wrap-status/{btc_tx_id}`
   - Allows frontend to monitor wrapping progress

6. Wrap history:
   - Endpoint: `/wrap-history/{wallet_id}`
   - Allows frontend to query wrap history

Rationale: This process ensures secure, traceable conversion from BTC to WBTC with proper status tracking and error handling.

## Unwrap Logic (WBTC to BTC)

The unwrapping process converts WBTC back to BTC:

1. User Action:
   - Frontend displays an ETH address (`Bridge-Delegator-ETH-Addr`)
   - User transfers WBTC to `Bridge-Delegator-ETH-Addr`
   - User confirms transfer in the app

2. Initiation:
   - Endpoint: `/initiate-unwrap`
   - Payload: signed ETH transaction to burn WBTC, with `wrp:wallet_id-btc_receiving_address` in calldata
   - Processing:
     a. extract `Bridge-Delegator-ETH-Addr`, `amount`, `wallet_id` and `btc_receiving_address` from the signed ETH transaction.
     b. broadcast the signed ETH transaction, and insert a record in DB with status `INITIATED`, the record should contain `Bridge-Delegator-ETH-Addr`, `amount`, `wallet_id`, `eth_tx_hash` and `btc_receiving_address`.
     c. return a eth_tx_hash to frontend

3. Background Processing:
   - For `INITIATED` status:
     a. if `eth_tx_hash` is not confirmed, skip and check again later.
     b. extract the tx details and update the record with new details.
     c. if amount meets the minimum unwrap amount:
        - Generate signed BTC transaction to `receiving_btc_address`
        - Include `Bridge-Delegator-ETH-Addr` and wallet ID in OP_RETURN output
        - Broadcast transaction
        - Insert field `btc_unwrapping_tx_hash` to record 
        - Set status to `BROADCASTED`
   - For `BROADCASTED` status:
     - Check BTC transaction confirmation
     - If confirmed, update status to `COMPLETED`

4. Status Checking:
   - Endpoint: `/unwrap-status/{eth_tx_hash}`
   - Allows frontend to monitor unwrapping progress

5. Unwrap history:
   - Endpoint: `/unwrap-history/{wallet_id}`
   - Allows frontend to query unwrap history

Rationale: This process ensures secure conversion from WBTC to BTC, handles potential transaction delays, and provides clear status tracking throughout the unwrapping process.

## Fee

### Wrap Fee

1. total: BTC transaction fee (0.0001 BTC) + ETH transaction fee (100 WBTC)

BTC transaction fee is included in the BTC transaction constructed in frontend.

ETH transaction fee is charged when MINTING. Because the fee is paid in ETH by contract owner and the amount is unknown, the fee is fixed at 100 WBTC and deducted from the amount of WBTC minted. 

2. get Fee
   - Endpoint: `/wrap-fee`
   - Input: None
   - Output: 

      ```json
      {
        "btc_fee": 0.0001,
        "eth_fee_in_wbtc": 100
      }
      ```

### Unwrap Fee

total: ETH transaction fee (unknown) + BTC transaction fee (0.0001 BTC)

ETH transaction fee is included in the ETH transaction constructed in frontend.

BTC transaction fee is included in the BTC transaction constructed in backend.

2. get Fee
   - Endpoint: `/unwrap-fee`
   - Input: None
   - Output: 
   
      ```json
      {
        "btc_fee": 0.0001,
        "eth_fee": 0
      }
      ```


```bash
python client.py wrap --amount 0.1 --wallet-id your_wallet_id
python client.py unwrap --amount 0.1 --wallet-id your_wallet_id
python client.py wrap-status --tx-id your_btc_tx_id
python client.py unwrap-status --tx-id your_eth_tx_hash
python client.py wrap-history --wallet-id your_wallet_id
python client.py unwrap-history --wallet-id your_wallet_id
```

## TODO:

- [ ] design the status transition diagram
- [ ] how to get the balance of WBTC in contract on chain