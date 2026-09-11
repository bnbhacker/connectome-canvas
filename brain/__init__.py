"""Connectome Canvas — the brain half.

graph.py     builds build/graph.npz from the FlyEM male CNS connectome (CC-BY 4.0)
sim.py       leaky integrate-and-fire over that graph, per-cell-type gains
retina.py    hex-column sampling of the canvas into lamina L1/L2
motor.py     descending-neuron readout -> brush command
palette.py   population activity -> colour
surrogate.py a clearly labelled stand-in graph for development without the connectome
"""
