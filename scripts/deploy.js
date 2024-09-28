require('dotenv').config();
const hre = require("hardhat");

const MaxGasPrice = hre.ethers.parseUnits('50', 'gwei');
async function main() {
    // Compile the contracts
    await hre.run('compile');

    // Get the deployer's signer
    const [deployer] = await hre.ethers.getSigners();
    console.log("Deploying contracts with the account:", deployer.address);

    const WBTC = await hre.ethers.getContractFactory("WBTC");
    
    // Set gas limit and price
    const gasLimit = 3200000; // Adjust this value as needed
    const feeData = await hre.ethers.provider.getFeeData();
    const gasPrice = feeData.gasPrice;
    console.log("Current gas price:", hre.ethers.formatUnits(gasPrice, "gwei"), "gwei");

    const finalGasPrice = gasPrice > MaxGasPrice ? MaxGasPrice : gasPrice;

    console.log("Using gas price:", hre.ethers.formatUnits(finalGasPrice, "gwei"), "gwei");

    try {
        console.log("Attempting to deploy WBTC contract...");
        
        // Remove the separate implementation deployment
        // Deploy the proxy directly
        console.log("Deploying proxy contract...");
        const wbtcProxy = await hre.upgrades.deployProxy(WBTC, [deployer.address], { 
            initializer: 'initialize',
            txOverrides: {
                gasLimit: gasLimit,
                gasPrice: finalGasPrice
            },
            kind: "uups",
            timeout: 24*60*60*1000
        });

        console.log("Waiting for proxy deployment transaction to be mined...");
        await wbtcProxy.waitForDeployment();
        const proxyAddress = await wbtcProxy.getAddress();
        console.log("WBTC proxy deployed to:", proxyAddress);

        // Verify the contract is actually deployed
        const code = await hre.ethers.provider.getCode(proxyAddress);
        if (code === '0x') {
            throw new Error("Contract deployment failed - no code at contract address");
        } else {
            console.log("Contract code verified at address:", proxyAddress);
        }
    } catch (error) {
        console.error("Deployment failed:", error);
        if (error.transaction) {
            console.log("Failed transaction:", error.transaction);
        }
        throw error;
    }
}

main()
    .then(() => process.exit(0))
    .catch((error) => {
        console.error(error);
        process.exit(1);
    });