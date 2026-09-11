// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {ERC721URIStorage} from "@openzeppelin/contracts/token/ERC721/extensions/ERC721URIStorage.sol";
import {ERC2981} from "@openzeppelin/contracts/token/common/ERC2981.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @title Connectome Canvas
/// @notice One token per sitting of a simulated fruit-fly nervous system.
///
///         Two roles. The *owner* is the keeper: a person's wallet that manages
///         the collection on marketplaces, receives royalties and can change the
///         painter. The *painter* is the automated wallet the studio signs with;
///         it can do exactly one thing — mint. Next to every token the contract
///         stores the SHA-256 of the PNG and the seed of the sitting, so anyone
///         with the graph can replay the picture and check the hash against the
///         chain rather than against a website.
contract ConnectomeCanvas is ERC721URIStorage, ERC2981, Ownable {
    uint256 public nextId = 1;
    address public painter;

    struct Provenance {
        bytes32 pngSha256;
        uint64 seed;
        uint64 mintedAt;
    }

    mapping(uint256 => Provenance) public provenance;

    event Painted(uint256 indexed tokenId, address indexed to, string uri, bytes32 pngSha256, uint64 seed);
    event PainterChanged(address indexed previous, address indexed current);

    error NotPainter(address caller);

    constructor(address keeper, address painter_, address royaltyReceiver)
        ERC721("Connectome Canvas", "CANVAS")
        Ownable(keeper)
    {
        painter = painter_;
        _setDefaultRoyalty(royaltyReceiver, 500); // 5 %
        emit PainterChanged(address(0), painter_);
    }

    modifier onlyPainter() {
        if (msg.sender != painter && msg.sender != owner()) revert NotPainter(msg.sender);
        _;
    }

    /// @notice Mint one finished sitting. Only the painter (or the keeper) may call.
    function mint(address to, string calldata uri, bytes32 pngSha256, uint64 seed)
        external
        onlyPainter
        returns (uint256 id)
    {
        id = nextId++;
        _safeMint(to, id);
        _setTokenURI(id, uri);
        provenance[id] = Provenance({pngSha256: pngSha256, seed: seed, mintedAt: uint64(block.timestamp)});
        emit Painted(id, to, uri, pngSha256, seed);
    }

    /// @notice Rotate the automated painter wallet.
    function setPainter(address painter_) external onlyOwner {
        emit PainterChanged(painter, painter_);
        painter = painter_;
    }

    /// @notice Repoint a token's metadata (e.g. when moving from a site URL to IPFS). Keeper only.
    function setTokenURI(uint256 tokenId, string calldata uri) external onlyOwner {
        _setTokenURI(tokenId, uri);
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
