// Optional: create an OpenSea listing (a signed Seaport order) for one Connectome Canvas token
// on Robinhood Chain from the command line. Listing by hand on opensea.io needs none of this.
//
//   node list.mjs --contract 0x... --token 12 --price 0.02 --days 30 --keystore ~/.connectome-canvas/keystore.json
//
// The keystore passphrase is read from stdin. OPENSEA_API_KEY must be set.
// Prints one JSON line: { orderHash, url, expires }.

import { readFileSync } from "node:fs";
import { OpenSeaSDK } from "opensea-js";
import { ethers } from "ethers";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, arr) => {
    if (a.startsWith("--")) acc.push([a.slice(2), arr[i + 1]]);
    return acc;
  }, [])
);

const RPC = process.env.CANVAS_RPC || "https://rpc.mainnet.chain.robinhood.com";
const CHAIN = "robinhood"; // OpenSea's slug for Robinhood Chain (chain id 4663)
const SITE = "https://opensea.io/assets/robinhood";

if (!process.env.OPENSEA_API_KEY) throw new Error("OPENSEA_API_KEY is not set");

const password = readFileSync(0, "utf8").split(/\r?\n/)[0];
const keystore = readFileSync(args.keystore, "utf8");
const wallet = await ethers.Wallet.fromEncryptedJson(keystore, password);
const provider = new ethers.JsonRpcProvider(RPC, 4663);
const signer = wallet.connect(provider);

const sdk = new OpenSeaSDK(signer, { chain: CHAIN, apiKey: process.env.OPENSEA_API_KEY });
const days = Number(args.days || 30);
const expirationTime = Math.round(Date.now() / 1000 + days * 86400);

const order = await sdk.createListing({
  asset: { tokenId: String(args.token), tokenAddress: args.contract },
  accountAddress: wallet.address,
  startAmount: args.price,
  expirationTime,
});

console.log(JSON.stringify({
  orderHash: order.orderHash,
  url: `${SITE}/${args.contract}/${args.token}`,
  expires: expirationTime,
}));
