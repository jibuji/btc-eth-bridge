```js
   const WBTC = await ethers.getContractFactory("WBTC");
   const wbtc = await WBTC.attach("0x5FbDB2315678afecb367f032d93F642f64180aa3");
   const [owner] = await ethers.getSigners();
   await wbtc.mint(owner.address, ethers.utils.parseEther("1"));
   const balance = await wbtc.balanceOf(owner.address);
   console.log("Balance:", ethers.utils.formatEther(balance));
```