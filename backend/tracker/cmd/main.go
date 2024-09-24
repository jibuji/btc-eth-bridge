package main

import (
	"database/sql"
	"fmt"
	"log"
	"math/big"
	"os"
	"strconv"
	"time"

	"context"
	"strings"

	"github.com/ethereum/go-ethereum"
	"github.com/ethereum/go-ethereum/accounts/abi"
	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/ethclient"
	"github.com/joho/godotenv"
	_ "github.com/mattn/go-sqlite3"
)

const (
	batchSize = 1000
)

type Holder struct {
	Address string
	Balance *big.Int
}

func main() {
	// Load .env file
	err := godotenv.Load()
	if err != nil {
		log.Fatalf("Error loading .env file: %v", err)
	}

	// Read WBTC address from environment
	wbtcAddress := os.Getenv("WBTC_ADDRESS")
	if wbtcAddress == "" {
		log.Fatalf("WBTC_ADDRESS not set in environment")
	}

	// Connect to Ethereum node (replace with your Infura URL or local node)
	client, err := ethclient.Dial(os.Getenv("ETHEREUM_NODE_URL"))
	if err != nil {
		log.Fatalf("Failed to connect to the Ethereum client: %v", err)
	}

	// Connect to database (PostgreSQL or SQLite)
	db, err := connectToDatabase()
	if err != nil {
		log.Fatalf("Unable to connect to database: %v", err)
	}
	defer db.Close()

	// Create table if not exists
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS wbtc_holders (
			address TEXT PRIMARY KEY,
			balance TEXT
		)
	`)
	if err != nil {
		log.Fatalf("Failed to create table: %v", err)
	}

	// Create table for last processed block
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS last_processed_block (
			id INTEGER PRIMARY KEY CHECK (id = 1),
			block_number INTEGER NOT NULL
		)
	`)
	if err != nil {
		log.Fatalf("Failed to create last_processed_block table: %v", err)
	}

	// Start updating holders in a separate goroutine
	go updateHolders(client, db, wbtcAddress)

	// Fetch and display holders
	fetchAndDisplayHolders(db)

	// Keep the main function running
	select {}
}

func connectToDatabase() (*sql.DB, error) {
	dbType := os.Getenv("DB_TYPE")
	dbURL := os.Getenv("DATABASE_URL")

	if dbType == "sqlite" {
		return sql.Open("sqlite3", dbURL)
	} else {
		return sql.Open("postgres", dbURL)
	}
}

func updateHolders(client *ethclient.Client, db *sql.DB, wbtcAddress string) {
	// Parse the contract ABI
	contractABI, err := abi.JSON(strings.NewReader(`[
		{"anonymous":false,"inputs":[{"indexed":true,"name":"from","type":"address"},{"indexed":true,"name":"to","type":"address"},{"indexed":false,"name":"value","type":"uint256"}],"name":"Transfer","type":"event"},
		{"anonymous":false,"inputs":[{"indexed":true,"name":"from","type":"address"},{"indexed":false,"name":"amount","type":"uint256"},{"indexed":false,"name":"data","type":"bytes"}],"name":"TokensBurned","type":"event"}
	]`))
	if err != nil {
		log.Fatalf("Failed to parse contract ABI: %v", err)
	}

	contractAddress := common.HexToAddress(wbtcAddress)
	transferSig := []byte("Transfer(address,address,uint256)")
	burnSig := []byte("TokensBurned(address,uint256,bytes)")
	transferTopic := crypto.Keccak256Hash(transferSig)
	burnTopic := crypto.Keccak256Hash(burnSig)

	query := ethereum.FilterQuery{
		Addresses: []common.Address{contractAddress},
		Topics:    [][]common.Hash{{transferTopic, burnTopic}},
	}

	// Get the last processed block number
	var lastProcessedBlock uint64
	err = db.QueryRow("SELECT block_number FROM last_processed_block WHERE id = 1").Scan(&lastProcessedBlock)
	if err != nil {
		if err == sql.ErrNoRows {
			// If no row exists, insert initial value from environment
			startingBlock, err := strconv.ParseUint(os.Getenv("STARTING_BLOCK"), 10, 64)
			if err != nil {
				log.Fatalf("Invalid STARTING_BLOCK in environment: %v", err)
			}
			_, err = db.Exec("INSERT INTO last_processed_block (id, block_number) VALUES (1, ?)", startingBlock)
			if err != nil {
				log.Fatalf("Failed to insert initial last processed block: %v", err)
			}
			lastProcessedBlock = startingBlock
		} else {
			log.Fatalf("Failed to get last processed block: %v", err)
		}
	}

	// Get the latest block number
	latestBlock, err := client.BlockNumber(context.Background())
	if err != nil {
		log.Fatalf("Failed to get latest block number: %v", err)
	}

	// Process events
	for {
		fromBlock := lastProcessedBlock + 1
		toBlock := fromBlock + 99 // Process 100 blocks at a time
		if toBlock > latestBlock {
			toBlock = latestBlock
		}

		query.FromBlock = big.NewInt(int64(fromBlock))
		query.ToBlock = big.NewInt(int64(toBlock))

		logs, err := client.FilterLogs(context.Background(), query)
		if err != nil {
			log.Printf("Failed to filter logs: %v", err)
			time.Sleep(15 * time.Second)
			continue
		}

		for _, vLog := range logs {
			switch vLog.Topics[0].Hex() {
			case transferTopic.Hex():
				handleTransferEvent(contractABI, db, vLog)
			case burnTopic.Hex():
				handleTokensBurnedEvent(contractABI, db, vLog)
			}
		}

		// Update the last processed block
		_, err = db.Exec("UPDATE last_processed_block SET block_number = ? WHERE id = 1", toBlock)
		if err != nil {
			log.Printf("Failed to update last processed block: %v", err)
		}

		lastProcessedBlock = toBlock

		if toBlock == latestBlock {
			// Wait before checking for new blocks
			time.Sleep(15 * time.Second)
			latestBlock, err = client.BlockNumber(context.Background())
			if err != nil {
				log.Printf("Failed to get latest block number: %v", err)
				time.Sleep(15 * time.Second)
				continue
			}
		}
	}
}

func handleTransferEvent(contractABI abi.ABI, db *sql.DB, vLog types.Log) {
	var transferEvent struct {
		From  common.Address
		To    common.Address
		Value *big.Int
	}
	err := contractABI.UnpackIntoInterface(&transferEvent, "Transfer", vLog.Data)
	if err != nil {
		log.Printf("Failed to unpack Transfer event: %v", err)
		return
	}

	transferEvent.From = common.HexToAddress(vLog.Topics[1].Hex())
	transferEvent.To = common.HexToAddress(vLog.Topics[2].Hex())
	amount := (*transferEvent.Value).Int64()
	fmt.Printf("Handling transfer event: %v to %v, amount: %v\n", transferEvent.From.Hex(), transferEvent.To.Hex(), amount)
	// Update balances in the database
	updateBalance(db, transferEvent.From.Hex(), new(big.Int).Neg(transferEvent.Value))
	updateBalance(db, transferEvent.To.Hex(), transferEvent.Value)
}

func handleTokensBurnedEvent(contractABI abi.ABI, db *sql.DB, vLog types.Log) {
	var burnEvent struct {
		From   common.Address
		Amount *big.Int
		Data   []byte
	}
	err := contractABI.UnpackIntoInterface(&burnEvent, "TokensBurned", vLog.Data)
	if err != nil {
		log.Printf("Failed to unpack TokensBurned event: %v", err)
		return
	}

	burnEvent.From = common.HexToAddress(vLog.Topics[1].Hex())
	amount := (*burnEvent.Amount).Int64()
	fmt.Printf("Handling tokens burned event: %v, amount: %v\n", burnEvent.From.Hex(), amount)
	// Update balance in the database (subtract burned amount)
	// updateBalance(db, burnEvent.From.Hex(), new(big.Int).Neg(burnEvent.Amount))
}

func updateBalance(db *sql.DB, address string, amount *big.Int) {
	// Get current balance
	var balanceStr string
	err := db.QueryRow("SELECT balance FROM wbtc_holders WHERE address = ?", address).Scan(&balanceStr)
	if err != nil && err != sql.ErrNoRows {
		log.Printf("Failed to query balance: %v", err)
		return
	}

	var balance *big.Int
	if err == sql.ErrNoRows {
		balance = big.NewInt(0)
	} else {
		balance, _ = new(big.Int).SetString(balanceStr, 10)
	}

	// Update balance
	newBalance := new(big.Int).Add(balance, amount)

	// Insert or update the database
	_, err = db.Exec(`
		INSERT INTO wbtc_holders (address, balance)
		VALUES (?, ?)
		ON CONFLICT(address) DO UPDATE SET balance = ?
	`, address, newBalance.String(), newBalance.String())
	if err != nil {
		log.Printf("Failed to update balance: %v", err)
	}
}

func fetchAndDisplayHolders(db *sql.DB) {
	offset := 0
	for {
		rows, err := db.Query(`
			SELECT address, balance FROM wbtc_holders
			ORDER BY CAST(balance AS DECIMAL) DESC
			LIMIT ? OFFSET ?
		`, batchSize, offset)
		if err != nil {
			log.Fatalf("Failed to query holders: %v", err)
		}
		defer rows.Close()

		holders := []Holder{}
		for rows.Next() {
			var holder Holder
			var balanceStr string
			err := rows.Scan(&holder.Address, &balanceStr)
			if err != nil {
				log.Fatalf("Failed to scan row: %v", err)
			}
			holder.Balance, _ = new(big.Int).SetString(balanceStr, 10)
			holders = append(holders, holder)
		}

		if len(holders) == 0 {
			break
		}

		for _, holder := range holders {
			fmt.Printf("Address: %s, Balance: %s\n", holder.Address, holder.Balance.String())
		}

		offset += batchSize
	}
}
