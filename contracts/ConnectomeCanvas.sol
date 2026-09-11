// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {ERC2981} from "@openzeppelin/contracts/token/common/ERC2981.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Strings} from "@openzeppelin/contracts/utils/Strings.sol";

/// @title Canvas Fly
/// @notice One token per sitting of a simulated fruit-fly nervous system, 5 000 at most.
///
///         Two roles. The *owner* is the keeper: a person's wallet that manages
///         the collection on marketplaces, receives royalties and can change the
///         painter, the base URI and the royalty. The *painter* is the automated
///         wallet the studio signs with; it can do exactly one thing — mint.
///
///         Metadata lives at `baseURI + tokenId` (the keeper can move it, e.g. to
///         IPFS, with setBaseURI or a per-token override). Next to every token the
///         contract stores the SHA-256 of the PNG and the seed of the sitting, so
///         anyone with the graph can replay the picture and check the hash against
///         the chain rather than against a website.
contract ConnectomeCanvas is ERC721, ERC2981, Ownable {
    using Strings for uint256;

    uint256 public immutable maxSupply;
    uint256 public nextId = 1;
    address public painter;
    string private _baseTokenURI;
    mapping(uint256 => string) private _tokenURIOverride;

    struct Provenance {
        bytes32 pngSha256;
        uint64 seed;
        uint64 mintedAt;
    }

    mapping(uint256 => Provenance) public provenance;

    event Painted(uint256 indexed tokenId, address indexed to, bytes32 pngSha256, uint64 seed);
    event PainterChanged(address indexed previous, address indexed current);
    event BaseURIChanged(string baseURI);

    error NotPainter(address caller);
    error SoldOut();

    constructor(address keeper, address painter_, address royaltyReceiver, uint256 maxSupply_, string memory baseURI_)
        ERC721("Canvas Fly", "CFLY")
        Ownable(keeper)
    {
        painter = painter_;
        maxSupply = maxSupply_;
        _baseTokenURI = baseURI_;
        _setDefaultRoyalty(royaltyReceiver, 500); // 5 %
        emit PainterChanged(address(0), painter_);
        emit BaseURIChanged(baseURI_);
    }

    modifier onlyPainter() {
        if (msg.sender != painter && msg.sender != owner()) revert NotPainter(msg.sender);
        _;
    }

    /// @notice Mint one finished sitting. Only the painter (or the keeper) may call.
    function mint(address to, bytes32 pngSha256, uint64 seed) external onlyPainter returns (uint256 id) {
        id = nextId;
        if (id > maxSupply) revert SoldOut();
        nextId = id + 1;
        _safeMint(to, id);
        provenance[id] = Provenance({pngSha256: pngSha256, seed: seed, mintedAt: uint64(block.timestamp)});
        emit Painted(id, to, pngSha256, seed);
    }

    /// @notice How many sittings have been minted so far.
    function totalMinted() external view returns (uint256) {
        return nextId - 1;
    }

    function tokenURI(uint256 tokenId) public view override returns (string memory) {
        _requireOwned(tokenId);
        string memory o = _tokenURIOverride[tokenId];
        if (bytes(o).length != 0) return o;
        return string.concat(_baseTokenURI, tokenId.toString());
    }

    // ---- keeper controls -------------------------------------------------------

    function setPainter(address painter_) external onlyOwner {
        emit PainterChanged(painter, painter_);
        painter = painter_;
    }

    function setBaseURI(string calldata baseURI_) external onlyOwner {
        _baseTokenURI = baseURI_;
        emit BaseURIChanged(baseURI_);
    }

    /// @notice Pin one token's metadata somewhere else (e.g. IPFS) without touching the rest.
    function setTokenURI(uint256 tokenId, string calldata uri) external onlyOwner {
        _requireOwned(tokenId);
        _tokenURIOverride[tokenId] = uri;
    }

    function setRoyalty(address receiver, uint96 bps) external onlyOwner {
        _setDefaultRoyalty(receiver, bps);
    }

    function supportsInterface(bytes4 interfaceId) public view override(ERC721, ERC2981) returns (bool) {
        return super.supportsInterface(interfaceId);
    }
}
