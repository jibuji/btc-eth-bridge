// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

contract WBTB is ERC20, Ownable {
    constructor(address deployer) ERC20("Wrapped Bitbi", "WBTB") Ownable(deployer) {}

    function mint(address to, uint256 amount) external onlyOwner {
        _mint(to, amount);
        // _mint already emits a Transfer event
    }

    event TokensBurned(address indexed from, uint256 amount, bytes data);

    function burn(uint256 amount, bytes calldata data) external {
        _burn(msg.sender, amount);
        // _burn already emits a Transfer event
        // Emit an event with the attached data
        emit TokensBurned(msg.sender, amount, data);
    }

    // Override the decimals function to set the token unit
    function decimals() public view virtual override returns (uint8) {
        return 8; // Set to 8 decimals, for example
    }
}