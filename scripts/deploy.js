require('dotenv').config();
const { ethers, run } = require("hardhat");

async function main() {
    // Compile the contracts
    await run('compile');
    // Get the private key from the .env file
    const privateKey = process.env.PRIVATE_KEY;
    if (!privateKey) {
        throw new Error("Please set your PRIVATE_KEY in a .env file");
    }

    // Create a wallet instance from the private key
    const wallet = new ethers.Wallet(privateKey, ethers.provider);

    console.log("Deploying contracts with the account:", wallet.address);

    const WBTC = await ethers.getContractFactory("WBTC");
    const wbtc = await WBTC.deploy(wallet.address);
    await wbtc.deployed();

    console.log("WBTC deployed to:", wbtc.address);
}

main()
    .then(() => process.exit(0))
    .catch((error) => {
        console.error(error);
        process.exit(1);
    });