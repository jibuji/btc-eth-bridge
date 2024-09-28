// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts-upgradeable/token/ERC20/ERC20Upgradeable.sol";
import "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

contract WBTC is Initializable, ERC20Upgradeable, OwnableUpgradeable, UUPSUpgradeable {
    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(address deployer) initializer public {
        __ERC20_init("Wrapped Bitcoin", "WBTC");
        __Ownable_init(deployer);
        __UUPSUpgradeable_init();
    }

    function mint(address to, uint256 amount) external onlyOwner {
        _mint(to, amount);
    }

    event TokensBurned(address indexed from, uint256 amount, bytes data);

    function burn(uint256 amount, bytes calldata data) external {
        _burn(msg.sender, amount);
        emit TokensBurned(msg.sender, amount, data);
    }

    function decimals() public view virtual override returns (uint8) {
        return 8;
    }

    function _authorizeUpgrade(address newImplementation)
        internal
        onlyOwner
        override
    {}
}