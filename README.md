# Connectome Canvas

**A real fruit-fly nervous system paints. The paintings are minted.**

165,122 neurons. 10,228,000 signed connections. Every one of them traced from electron
microscope images of an actual male *Drosophila melanogaster* — the male CNS connectome
released in September 2026 by HHMI Janelia FlyEM, the Cambridge Connectomics Group and
Google Research, CC-BY 4.0. Not invented, not sampled from a distribution, not a neural
network "inspired by" a brain.

The retina looks at a canvas, all 165,122 neurons integrate, the descending neurons a fly
walks with move a brush, and the shape of the activity picks the colour. The fly paints
what it sees and sees what it paints. When a sitting's brain time is spent, the picture is
hashed, minted as an ERC-721 on Base with the hash and the seed written next to the token,
and listed on OpenSea. Nobody draws.

Site: `site/` · Studio server: `python run.py serve` · Contract: `contracts/ConnectomeCanvas.sol`

## What actually happens, once per window of brain time

1. **See.** A 256 px patch of the canvas around the brush is sampled through 892 retinotopic
   hex columns into L1 (ON) and L2 (OFF) — the lamina monopolar cells directly behind
   photoreceptors R1–R6. The column map is the release's own `assignedOlHex1/2` for one eye,
   so retinotopy is the fly's, not a projection we invented. 892 L1 and 893 L2 cells are wired.
2. **Integrate.** 10 ms of brain time in 0.2 ms steps. Leaky integrate-and-fire after
   Shiu et al. 2024: rest −52 mV, threshold −45 mV, 20 ms tau, 2.2 ms refractory, one spike adds
   `sign × synapses × 0.275 mV` to each target. Cost tracks how many neurons fire, not the population.
3. **Move.** Rates of the real descending neurons, smoothed over a few windows:

   | neuron | in a fly | here |
   |---|---|---|
   | DNa02 L vs R | steering by left/right asymmetry | heading turns by (R−L)/(R+L), up to 0.55 rad |
   | DNa01 | forward walking | step 2.5–28 px, stroke width 2–16 px |
   | MDN | the moonwalker neuron, walking backwards | the brush reverses |
   | DNp09 | freezing | the brush lifts, the stroke breaks |

4. **Colour.** Hue is the optic-lobe / central-brain / VNC mix of the window's spikes,
   saturation the Kenyon-cell share, lightness the rate. A person chose that mapping; it is fixed.
5. **Learn.** Fresh canvas is the reward, the edge is the punishment, delivered through PAM and
   PPL1 dopamine neurons onto the KC→MBON synapses — depression only, the rule a fly uses.
   Reward-side and punishment-side MBONs are found by comparing their PAM and PPL1 input in the
   released wiring, not typed in.
6. **Score.** Everything above is appended to the sitting's *score*: brush position, lift, colour,
   width, DN rates, spike counts, a sample of which neurons fired, the global gain. The score
   replays the painting in any browser with the brain switched off (`site/demo.html`).
7. **Mint.** PNG → SHA-256 → IPFS. `mint(to, uri, pngSha256, seed)` stores the hash and the
   32-bit seed on chain. A Seaport listing goes to OpenSea. `python run.py replay N` re-runs the
   seed and reports MATCH or DIFFERENT.

## What is not the fly, stated plainly

- **The brush is a translation.** Steering, forward, backward and stop are read from real
  descending neurons; "brush down" and stroke width are a mapping a person wrote.
- **Colour is not a fly decision.** A fly has no notion of paint.
- **The reward is invented.** The circuit, the plasticity site and the direction of the rule are
  measured. What a fly is rewarded by is sugar, not by covering fresh canvas.
- **Three things are added to the anatomy, all logged, all seeded:**
  *background noise* — 0.1 % of neurons get a 2 mV kick each step so the network never falls silent;
  *spike-frequency adaptation* — each spike raises that neuron's own threshold by 3 mV, decaying over
  200 ms, which nearly every real neuron does and Shiu's LIF does not;
  *synaptic scaling* — a neuron's summed incoming |weight| is capped at 45 mV, the way real neurons
  keep their total drive in range. Without the last two the dense, net-excitatory connectome
  seizes within milliseconds of steady retinal input and every neuron fires at 400 Hz.
  On top of these one **global gain** is nudged slowly to hold the population between 1 and 10 Hz;
  its value is in every window of every score.
- **Gains are untrained.** Every per-cell-type gain is 1.0. Training them is a listed next step.
- **It has no goals.** A fly brain has no language and no plan. It cannot see the picture as a
  picture. Nobody is steering — that is the point, not a claim that it decided anything.

## Run it

```bash
pip install -r requirements.txt
python run.py fetch          # ~540 MB from storage.googleapis.com/flyem-male-cns — CC-BY, no account
python run.py build          # data/*.feather -> build/graph.npz  (165,122 neurons, ~12 s, 38 MB)
python run.py paint          # one headless sitting -> gallery/canvas-0001.png + site/recordings/canvas-0001.json
python run.py serve          # the studio: sittings back to back + the site at http://127.0.0.1:4660
```

No connectome yet? `python run.py surrogate` writes a random graph with the same annotation shape.
The site prints **SURROGATE GRAPH — NOT THE CONNECTOME** across every page while it is loaded, and
`mint` refuses to touch anything painted on it.

`python run.py describe` prints the columns the release files actually contain; `build` detects them
rather than assuming.

### The site

| page | what it is |
|---|---|
| `index.html` | the studio: live canvas, the fly's eye, neuron scatter, DN readout, palette, mushroom body, log |
| `demo.html` | the demonstration: a recorded sitting replayed stroke by stroke by the fly cursor — no live feed |
| `gallery.html` | every finished sitting with seed, hashes, token, tx and OpenSea links |
| `brain.html` | what is running, what is measured and what is chosen, sources |

The studio pages read `/api/*` from the server. Without a server the demonstration and the
gallery fall back to `site/recordings/`, which is static — the whole site deploys to Vercel or
GitHub Pages as files.

### Chain — the simple way (Robinhood Chain, listed by hand)

OpenSea indexes Robinhood Chain (chain id 4663, slug `robinhood`), and gas there costs a fraction
of a cent, so the cheapest honest pipeline is: the fly paints, the painter wallet mints the token
straight into the keeper's wallet, the keeper lists it on opensea.io with a signature.

```bash
python run.py wallet new                         # painter keystore at ~/.connectome-canvas/keystore.json; fund it with ~0.001 ETH on Robinhood Chain
cd tools && npm install && cd ..                 # OpenZeppelin for the compiler (py-solc-x, no Foundry needed)
python run.py deploy                             # dry run: compiles, estimates gas
python run.py deploy --live                      # deploys ConnectomeCanvas on Robinhood Chain -> CANVAS_CONTRACT
export CANVAS_CONTRACT=0x...  CANVAS_OWNER=0xYourWallet   # tokens are minted to CANVAS_OWNER
python run.py serve --sittings 1                 # the fly paints one sitting and rests
python run.py mint 1                             # dry run: shows the metadata
python run.py mint 1 --live                      # writes site/nft/1.json + png, mints token 1 to CANVAS_OWNER
python run.py publish                            # pushes site/ (gallery, score, nft metadata) to Vercel
```

Then on opensea.io: connect the `CANVAS_OWNER` wallet on Robinhood Chain → the token is in your
profile within minutes → *Sell* → price → sign. The first listing also asks for one `setApprovalForAll`
transaction (cents). No API key, no Pinata: with `CANVAS_METADATA=site` the image and JSON are served
from connectomecanvas.com/nft/, and the PNG hash plus the seed are on chain regardless.

`CANVAS_METADATA=ipfs` (Pinata) and `run.py list` (opensea-js, needs `OPENSEA_API_KEY`) remain for the
fully automatic mode: `CANVAS_MINT=1 CANVAS_LIST_ETH=0.02 python run.py serve`.

The private key exists only inside the encrypted keystore, outside the repository. It is never in
`.env`, never in an environment variable, never printed. The passphrase is asked for on the terminal
(or read from a file named by `CANVAS_KEYSTORE_PASSWORD_FILE` for unattended runs).

### Hosting

Two halves, two hosts:

| what | where | how |
|---|---|---|
| the site (`site/`) | Vercel, **connectomecanvas.com** | static files; `site/vercel.json` rewrites `/api/*` and `/gallery/*` to the studio |
| the studio (`server.py`) | Railway (or any Docker host), **studio.connectomecanvas.com** | `Dockerfile` — pulls the built graph from the `graph-v1.0` GitHub release instead of the 540 MB data, so the image builds in about a minute |

```bash
# site: deploy the folder, not the repo root (Windows: copy site/ to an ASCII path first)
cd site && vercel --prod
# studio: connect the repo in Railway → it reads railway.json + Dockerfile; set env from .env.example
```

DNS at the registrar: `@` A `76.76.21.21`, `www` CNAME `cname.vercel-dns.com`, `studio` CNAME → the
target Railway prints when you add the custom domain. Without the studio the site still works: the
demonstration and the gallery read `site/recordings/`, and the studio page says so.

## Layout

```
brain/      graph.py (release -> graph.npz), sim.py (LIF + adaptation + scaling), retina.py, motor.py,
            palette.py, mushroom.py, surrogate.py
canvas/     painter.py — the sitting: see / integrate / move / colour / learn / score
chain/      keystore.py, ipfs.py (Pinata), mint.py (web3, Base), opensea.py (-> tools/list.mjs)
contracts/  ConnectomeCanvas.sol (ERC-721 + ERC-2981, provenance per token), Deploy.s.sol
site/       the four pages, style.css, common.js, replay.js, recordings/
server.py   FastAPI: /api/info /api/state /api/frame.png /api/eye.png /api/neurons /api/gallery + static
run.py      the command line
```

## Credit

Connectome data © HHMI Janelia FlyEM, the Cambridge Connectomics Group and Google Research,
CC-BY 4.0 — see `NOTICE`. Model after Shiu et al. 2024 (Nature); retinotopic drive after
Lappalainen et al. 2024 (Nature). Wiring this connectome to a cursor through DNa02 / DNa01 / MDN /
DNp09 was done first, openly, by the flybrain project (MIT); this is an independent implementation
pointed at a canvas. Not affiliated with any of them, nor with OpenSea or Base.

Connectome Canvas is an art experiment, not an investment.
