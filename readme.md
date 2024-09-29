```js
   const WBTB = await ethers.getContractFactory("WBTB");
   const wbtb = await WBTB.attach("0x5FbDB2315678afecb367f032d93F642f64180aa3");
   const [owner] = await ethers.getSigners();
   await wbtb.mint(owner.address, ethers.utils.parseEther("1"));
   const balance = await wbtb.balanceOf(owner.address);
   console.log("Balance:", ethers.utils.formatEther(balance));
```
