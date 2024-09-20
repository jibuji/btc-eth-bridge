// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

contract WBTC is ERC20, Ownable {
    constructor(address deployer) ERC20("Wrapped Bitcoin", "WBTC") Ownable(deployer) {}

    function mint(address to, uint256 amount) external onlyOwner {
        _mint(to, amount);
    }

    event TokensBurned(address indexed from, uint256 amount, bytes data);

    function burn(uint256 amount, bytes calldata data) external {
        _burn(msg.sender, amount);
        
        // Emit an event with the attached data
        emit TokensBurned(msg.sender, amount, data);
    }
}