// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {ERC721URIStorage} from "@openzeppelin/contracts/token/ERC721/extensions/ERC721URIStorage.sol";
import {ERC2981} from "@openzeppelin/contracts/token/common/ERC2981.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @title Connectome Canvas
/// @notice One token per sitting of a simulated fruit-fly nervous system.
///         The painter wallet (owner) is the only minter. Next to every token
///         the contract stores the SHA-256 of the PNG and the seed of the
///         sitting, so anyone with the graph can replay the picture and check
///         the hash against the chain rather than against the website.
contract ConnectomeCanvas is ERC721URIStorage, ERC2981, Ownable {
    uint256 public nextId = 1;

    struct Provenance {
        bytes32 pngSha256;
        uint64 seed;
        uint64 mintedAt;
    }

    mapping(uint256 => Provenance) public provenance;

    event Painted(uint256 indexed tokenId, string uri, bytes32 pngSha256, uint64 seed);

    constructor(address painter, address royaltyReceiver)
        ERC721("Connectome Canvas", "CANVAS")
        Ownable(painter)
    {
        _setDefaultRoyalty(royaltyReceiver, 500); // 5 %
    }

    function mint(address to, string calldata uri, bytes32 pngSha256, uint64 seed)
        external
        onlyOwner
        returns (uint256 id)
    {
        id = nextId++;
        _safeMint(to, id);
        _setTokenURI(id, uri);
        provenance[id] = Provenance({pngSha256: pngSha256, seed: seed, mintedAt: uint64(block.timestamp)});
        emit Painted(id, uri, pngSha256, seed);
    }

    function setRoyalty(address receiver, uint96 bps) external onlyOwner {
        _setDefaultRoyalty(receiver, bps);
    }

    function supportsInterface(bytes4 interfaceId)
        public
        view
        override(ERC721URIStorage, ERC2981)
        returns (bool)
    {
        return super.supportsInterface(interfaceId);
    }
}
