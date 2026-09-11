// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {ConnectomeCanvas} from "../ConnectomeCanvas.sol";

/// forge script contracts/script/Deploy.s.sol --rpc-url $CANVAS_RPC --account painter --broadcast
///
/// `--account painter` uses a Foundry keystore (forge wallet import painter --interactive),
/// so no private key ever touches the shell or .env. KEEPER owns the contract and manages
/// the collection, PAINTER is the automated wallet that may only mint, ROYALTY is where
/// 5 % secondary royalties go (default: the keeper).
contract Deploy is Script {
    function run() external {
        address keeper = vm.envAddress("KEEPER");
        address painter = vm.envAddress("PAINTER");
        address royalty = vm.envOr("ROYALTY", keeper);
        vm.startBroadcast();
        ConnectomeCanvas c = new ConnectomeCanvas(keeper, painter, royalty);
        vm.stopBroadcast();
        console.log("ConnectomeCanvas deployed at", address(c));
        console.log("export CANVAS_CONTRACT=", address(c));
    }
}
