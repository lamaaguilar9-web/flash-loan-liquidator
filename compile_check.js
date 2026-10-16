const fs = require('fs');
const path = require('path');

// Load solc from workspace node_modules
const solcPath = path.resolve(__dirname, '..', 'bnb-invariant-shield', 'node_modules', 'solc');
const solc = require(solcPath);

const contractPath = path.resolve(__dirname, 'contracts', 'UniversalFlashExecutor.sol');
const source = fs.readFileSync(contractPath, 'utf8');

const input = {
    language: 'Solidity',
    sources: {
        'UniversalFlashExecutor.sol': { content: source }
    },
    settings: {
        optimizer: {
            enabled: true,
            runs: 200
        },
        viaIR: true,
        outputSelection: {
            '*': {
                '*': ['abi', 'evm.bytecode.object', 'evm.deployedBytecode.object']
            }
        }
    }
};

const output = JSON.parse(solc.compile(JSON.stringify(input)));

if (output.errors) {
    let hasError = false;
    output.errors.forEach(err => {
        console.log(err.formattedMessage);
        if (err.severity === 'error') hasError = true;
    });
    if (hasError) {
        process.exit(1);
    }
}

const compiled = output.contracts['UniversalFlashExecutor.sol']['UniversalFlashExecutor'];
const artifactDir = path.resolve(__dirname, 'contracts', 'artifacts');
if (!fs.existsSync(artifactDir)) {
    fs.mkdirSync(artifactDir, { recursive: true });
}

const artifactPath = path.join(artifactDir, 'UniversalFlashExecutor.json');
fs.writeFileSync(
    artifactPath,
    JSON.stringify({
        contractName: 'UniversalFlashExecutor',
        compilerVersion: solc.version(),
        abi: compiled.abi,
        bytecode: '0x' + compiled.evm.bytecode.object,
        deployedBytecodeSize: compiled.evm.deployedBytecode.object.length / 2
    }, null, 2),
    'utf8'
);

console.log(`SUCCESS: UniversalFlashExecutor.sol compiled cleanly with 0 errors! (Bytecode size: ${compiled.evm.deployedBytecode.object.length / 2} bytes, solc ${solc.version()})`);
