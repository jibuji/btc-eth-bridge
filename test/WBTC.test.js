const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("WBTC", function () {
  let WBTC, wbtc, owner, addr1, addr2;

  beforeEach(async function () {
    [owner, addr1, addr2] = await ethers.getSigners();
    WBTC = await ethers.getContractFactory("WBTC");
    wbtc = await WBTC.deploy(owner.address);
    await wbtc.deployed();
  });

  describe("Deployment", function () {
    it("Should set the right owner", async function () {
      expect(await wbtc.owner()).to.equal(owner.address);
    });

    it("Should assign the total supply of tokens to the owner", async function () {
      const ownerBalance = await wbtc.balanceOf(owner.address);
      expect(await wbtc.totalSupply()).to.equal(ownerBalance);
    });
  });

  describe("Transactions", function () {
    it("Should mint tokens to address", async function () {
      await wbtc.mint(addr1.address, 50);
      const addr1Balance = await wbtc.balanceOf(addr1.address);
      expect(addr1Balance).to.equal(50);
    });

    it("Should burn tokens from address", async function () {
      await wbtc.mint(addr1.address, 50);
      await wbtc.burn(addr1.address, 30);
      const addr1Balance = await wbtc.balanceOf(addr1.address);
      expect(addr1Balance).to.equal(20);
    });

    it("Should fail if non-owner tries to mint", async function () {
      await expect(
        wbtc.connect(addr1).mint(addr2.address, 50)
      ).to.be.revertedWith("OwnableUnauthorizedAccount");
    });
  });
});