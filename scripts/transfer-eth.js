   // scripts/transfer-eth.js
   const { ethers } = require("hardhat");

   async function main() {
       // Get the list of accounts
       const [sender] = await ethers.getSigners();
       const receiverAddr = "0x14F911dDa85Ec705a96F0E5cE10aB17897018226";
       // Display the initial balances
       console.log("Sender balance:", (await sender.getBalance()).toString());

       // Define the transaction details
       const tx = {
           to: receiverAddr,
           value: ethers.utils.parseEther("10.0"), // Sending 1 ETH
       };

       // Send the transaction
       const txResponse = await sender.sendTransaction(tx);
       await txResponse.wait(); // Wait for the transaction to be mined

       // Display the final balances
       console.log("Sender balance:", (await sender.getBalance()).toString());
   }

   main()
       .then(() => process.exit(0))
       .catch((error) => {
           console.error(error);
           process.exit(1);
       });