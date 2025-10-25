# AIND Experiment: VrForaging + FIP

An repository for an experiment that acquires data from VrForaging and FIP.

## Getting started

1. Ensure the requirements of both repositories [Aind.Behavior.VrForaging ](https://github.com/AllenNeuralDynamics/Aind.Behavior.VrForaging)and [Aind.Physiology.Fip](https://github.com/AllenNeuralDynamics/Aind.Physiology.Fip/) are fulfilled. This repository requires uv to be installed!
2. Clone this repository
3. Run `./scripts/deploy.cmd`
4. Ensure `clabe.yml` is defined in your system. There is an example in `examples/clabe.yml` that can be used to get you going by copying it into `./local`.
5. `main.py` provides an easy way to launch the combined experiment. Feel free to change the `experiment` function to whatever fits your need.
6. Run the experiment by `uv run main.py <optional launcher parameters>`