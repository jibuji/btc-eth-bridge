require('dotenv').config();
const { ethers, run } = require("hardhat");

async function main() {
    // Compile the contracts
    await run('compile');

    // Get the first signer from the Hardhat runtime environment
    const [deployer] = await ethers.getSigners();

    console.log("Deploying contracts with the account:", deployer.address);

    const WBTB = await ethers.getContractFactory("WBTB");
    
    // Set the gas price (in wei)
    const currentGasPrice = await ethers.provider.getGasPrice();
    const maxGasPrice = ethers.utils.parseUnits("30", "gwei"); // Set max gas price to 100 Gwei
    const gasPrice = currentGasPrice.gt(maxGasPrice) ? maxGasPrice : currentGasPrice;
    
    console.log(`Current gas price: ${ethers.utils.formatUnits(currentGasPrice, "gwei")} Gwei`);
    console.log(`Using gas price: ${ethers.utils.formatUnits(gasPrice, "gwei")} Gwei`);

    const wbtb = await WBTB.deploy(deployer.address, { gasPrice: gasPrice, gasLimit: 2600000 });
    await wbtb.deployed();

    console.log("WBTB deployed to:", wbtb.address);

    // Verify the contract on Etherscan
    if (process.env.ETHERSCAN_API_KEY) {
        console.log("Verifying contract on Etherscan...");
        await run("verify:verify", {
            address: wbtb.address,
            constructorArguments: [deployer.address],
        });
        console.log("Contract verified on Etherscan");
    }
}

main()
    .then(() => process.exit(0))
    .catch((error) => {
        console.error(error);
        process.exit(1);
    });