// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {ConnectomeCanvas} from "../ConnectomeCanvas.sol";

/// forge script contracts/script/Deploy.s.sol --rpc-url $CANVAS_RPC --account painter --broadcast
///
/// `--account painter` uses a Foundry keystore (forge wallet import painter --interactive),
/// so no private key ever touches the shell or .env. PAINTER is the address that will
/// mint; ROYALTY is where 5 % secondary royalties go.
contract Deploy is Script {
    function run() external {
        address painter = vm.envAddress("PAINTER");
        address royalty = vm.envOr("ROYALTY", painter);
        vm.startBroadcast();
        ConnectomeCanvas c = new ConnectomeCanvas(painter, royalty);
        vm.stopBroadcast();
        console.log("ConnectomeCanvas deployed at", address(c));
        console.log("export CANVAS_CONTRACT=", address(c));
    }
}
