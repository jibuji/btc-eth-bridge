```js
   const WBTC = await ethers.getContractFactory("WBTC");
   const wbtc = await WBTC.attach("0x5FbDB2315678afecb367f032d93F642f64180aa3");
   const [owner] = await ethers.getSigners();
   await wbtc.mint(owner.address, ethers.utils.parseEther("1"));
   const balance = await wbtc.balanceOf(owner.address);
   console.log("Balance:", ethers.utils.formatEther(balance));
```

## wrap logic

The `/initiate-wrap` API endpoint is used to initiate the wrapping process. It takes a `WrapRequest` object as input, which includes the signed Bitcoin transaction and the Ethereum address to which the wrapped tokens will be sent.

The endpoint first attempts to broadcast the signed Bitcoin transaction. If successful, it creates a new transaction record in the database with the status set to `BROADCASTED`. If the broadcast fails, an error is returned.

The endpoint then returns a response with the Bitcoin transaction ID and a message indicating that the wrap has been initiated and the transaction has been broadcasted.

So the frontend can check the status of the background Wrapping progress by `/wrap-status/{btc_tx_id}`

## unwrap logic

The `/initial-unwrap` API endpoint is used to initiate the unwrapping process. The main logic:

1. client show an ETH address( called `Holding Addr` in the following ) that user will send WBTC to.
2. client request backend to check `Holding Addr` for WBTC, after confirmed, then call `burn` method of the bridge contract with the receiving BTC address as callee data. Then client sent the `burn` tx to backend.
3. backend listens all `burn` tx in the bridge contract, and sent btc back to the attached btc address.
4. client can check the status of unwrapping by request backend.