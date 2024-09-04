const crypto = require('crypto');

function generateRandomPrivateKey() {
    return '0x' + crypto.randomBytes(32).toString('hex');
}

const randomKey = generateRandomPrivateKey();
console.log(randomKey);
