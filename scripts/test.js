const ethers = require('ethers');
require('dotenv').config();
async function getWBTCBalance(address) {
    try {
        // Connect to the network where your contract is deployed
        const provider = new ethers.providers.JsonRpcProvider(process.env.SEPOLIA_RPC_URL);
        console.log("process.env.SEPOLIA_RPC_URL", process.env.SEPOLIA_RPC_URL);
        // The ABI for the balanceOf function
        const abi = [
            "function balanceOf(address account) view returns (uint256)",
            "function name() view returns (string)",
            "function symbol() view returns (string)"
        ];

        // Create a contract instance
        const wbtcContract = new ethers.Contract('0xc05E92506244e12C47aEa4E5B7F4b859488709b8', abi, provider);

        // Check if the contract exists
        const code = await provider.getCode(wbtcContract.address);
        if (code === '0x') {
            console.log(`No contract found at address ${wbtcContract.address}`);
            return;
        }

        // Try to get the token name and symbol
        try {
            const name = await wbtcContract.name();
            const symbol = await wbtcContract.symbol();
            console.log(`Contract found: ${name} (${symbol})`);
        } catch (error) {
            console.log('Could not retrieve token name and symbol');
        }

        // Call the balanceOf function
        const balance = await wbtcContract.balanceOf(address);

        console.log(`${address}: ${ethers.utils.formatUnits(balance, 8)} WBTC`);
    } catch (error) {
        console.error(`Error for address ${address}:`, error.message);
    }
}

// Example usage
getWBTCBalance('0x14F911dDa85Ec705a96F0E5cE10aB17897018226');
getWBTCBalance('0x0000000000000000000000000000000000000000');
getWBTCBalance('0xc05E92506244e12C47aEa4E5B7F4b859488709b8');
