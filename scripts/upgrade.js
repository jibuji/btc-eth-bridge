const { ethers, upgrades } = require("hardhat");

async function main() {
    const proxyAddress = "0xA11f0ae2a935144D64c8fd5C18001dB58790a976"; // Replace with your proxy address
    const WBTC = await ethers.getContractFactory("WBTC"); // New version of the contract
    const wbtc = await upgrades.upgradeProxy(proxyAddress, WBTC);
    console.log("WBTC upgraded successfully");
    console.log("Upgraded WBTC address:", wbtc);
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});