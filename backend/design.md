## Wallet ID Generation

A wallet ID is a unique identifier for a Bitcoin wallet, it is a base58 encoded string of `Bridge-Delegator-ETH-Addr`, which is implemented in frontend.

## Wrap Logic (BTB to WBTB)

The wrapping process converts BTB to WBTB through the following steps:

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
     - If confirmed: Mint WBTB, update status to `MINTING`, store minting tx hash
   - For `MINTING` status:
     - Check WBTB minting transaction confirmation
     - If confirmed: Update status to `COMPLETED`

5. Status Checking:
   - Endpoint: `/wrap-status/{btb_tx_id}`
   - Allows frontend to monitor wrapping progress

6. Wrap history:
   - Endpoint: `/wrap-history/{wallet_id}`
   - Allows frontend to query wrap history

Rationale: This process ensures secure, traceable conversion from BTB to WBTB with proper status tracking and error handling.

## Unwrap Logic (WBTB to BTB)

The unwrapping process converts WBTB back to BTB:

1. User Action:
   - Frontend displays an ETH address (`Bridge-Delegator-ETH-Addr`)
   - User transfers WBTB to `Bridge-Delegator-ETH-Addr`
   - User confirms transfer in the app

2. Initiation:
   - Endpoint: `/initiate-unwrap`
   - Payload: signed ETH transaction to burn WBTB, with `wrp:wallet_id-btb_receiving_address` in calldata
   - Processing:
     a. extract `Bridge-Delegator-ETH-Addr`, `amount`, `wallet_id` and `btb_receiving_address` from the signed ETH transaction.
     b. broadcast the signed ETH transaction, and insert a record in DB with status `INITIATED`, the record should contain `Bridge-Delegator-ETH-Addr`, `amount`, `wallet_id`, `eth_tx_hash` and `btb_receiving_address`.
     c. return a eth_tx_hash to frontend

3. Background Processing:
   - For `INITIATED` status:
     a. if `eth_tx_hash` is not confirmed, skip and check again later.
     b. extract the tx details and update the record with new details.
     c. if amount meets the minimum unwrap amount:
        - Generate signed BTB transaction to `receiving_btb_address`
        - Include `Bridge-Delegator-ETH-Addr` and wallet ID in OP_RETURN output
        - Broadcast transaction
        - Insert field `btb_unwrapping_tx_hash` to record 
        - Set status to `BROADCASTED`
   - For `BROADCASTED` status:
     - Check BTB transaction confirmation
     - If confirmed, update status to `COMPLETED`

4. Status Checking:
   - Endpoint: `/unwrap-status/{eth_tx_hash}`
   - Allows frontend to monitor unwrapping progress

5. Unwrap history:
   - Endpoint: `/unwrap-history/{wallet_id}`
   - Allows frontend to query unwrap history

Rationale: This process ensures secure conversion from WBTB to BTB, handles potential transaction delays, and provides clear status tracking throughout the unwrapping process.

## Fee and Bridge Information

To provide a consolidated view of the bridge information, including fees and contract details, we use a single endpoint:

### Bridge Info

- Endpoint: `/bridge-info`
- Method: GET
- Input: None
- Output: JSON containing WBTB contract ABI, wrap fee, and unwrap fee

```json
{
  "wbtb_contract_abi": [...],
  "wrap_fee": {
    "btb_fee": 0.0001,
    "eth_fee_in_wbtb": 100
  },
  "unwrap_fee": {
    "btb_fee": 0.0001,
    "eth_fee": 0.000123
  }
}
```

#### Wrap Fee Details

- Total wrap fee: BTB transaction fee (0.0001 BTB) + ETH transaction fee (100 WBTB)
- BTB transaction fee is included in the BTB transaction constructed in frontend.
- ETH transaction fee is charged when MINTING. Because the fee is paid in ETH by contract owner and the amount is unknown, the fee is fixed at 100 WBTB and deducted from the amount of WBTB minted.

#### Unwrap Fee Details

- Total unwrap fee: ETH transaction fee (variable) + BTB transaction fee (0.0001 BTB)
- ETH transaction fee is included in the ETH transaction constructed in frontend.
- BTB transaction fee is included in the BTB transaction constructed in backend.

Rationale: This consolidated endpoint provides all necessary information about the bridge, including contract details and fees, in a single request. This reduces the number of API calls needed and simplifies the process for frontend integration.

## TODO:

- [ ] design the status transition diagram
- [ ] how to get the balance of WBTB in contract on chain