# ZMART Analysis

[![tests](https://github.com/ZMB-UZH/ZMART-analysis/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/ZMB-UZH/ZMART-analysis/actions/workflows/test.yml)
[![python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![status](https://img.shields.io/badge/status-beta-yellow)](#status)

<table>
<tr>
<td width="170"><img src="docs/zmart-analysis-icon.png" width="150" alt="ZMART Analysis"></td>
<td valign="middle">

**ZMART Analysis** provides an image analysis engine built for smart microscopy. It allows you to trigger and scale image analysis when the data comes in, and handles conflicting dependencies, documentation and reproducibility.

It is part of **ZMART** (ZMB's Microscopy-Agnostic Research Toolkit), the tools we use for smart microscopy at the Center for Microscopy and Image Analysis (ZMB), University of Zurich.

</td>
</tr>
</table>


<sub><sub><br></sub></sub>

## The challenges of analyzing images real-time during smart microscopy

When building smart microscopy workflows, you are likely to run into the
following four challenges.

1. **Analysis over scopes.**<br>
   Analysis can run over a single image, a group, a compartment, a carrier or
   the whole experiment. In smart microscopy the data comes in piece by piece,
   so a step over a larger scope can only start once all its images are in.

2. **Reproducibility.**<br>
   Image analysis workflows need to be properly documented, reproducible and
   easily sharable. This is already a challenge in post-acquisition image
   analysis, and is even more tricky for smart microscopy workflows.

3. **Dependency conflicts.**<br>
   Analysis pipelines consist of multiple steps. Each step needs specific
   dependencies. These dependencies often conflict in
   the same environment they are executed in. This creates a real barrier for changing and getting your
   analysis up and running quickly.

4. **Latency.**<br>
   The microscope waits for feedback to move on, so the analysis needs to be
   finished as soon as possible, even though computational resources are
   often limited.

<sub><sub><br></sub></sub>

## Our Solution to These Challenges

We created the ZMART Analysis pipeline engine to address these barriers. It is
built with the aim to minimize input, configuration and friction as much as
possible.

1. **Analysis steps declare a scope.**<br>
   Each step declares over which scope its analysis needs to run: a single
   image, a group, a compartment, a carrier, or the experiment. An analysis
   step is triggered automatically as soon as the acquisition over the given scope
   is finished.

2. **Analysis pipelines are declared in YAML files.**<br>
   A YAML file defines which steps are run, in which order, and with which
   settings. You can reuse them as templates, so they are easy to adjust and
   document your analysis.

3. **Steps can be isolated and executed by different workers.**<br>
   Each step names the conda environment it needs, so dependency conflicts
   are avoided.

4. **Workers stay active and can run steps concurrently.**<br>
   Multiple workers can be initialized. They stay active and accept tasks as
   soon as they are free.

<sub><sub><br></sub></sub>

## Want to Give It a Try?

1. **[Use the engine](docs/1_use_the_engine/README.md)**
2. **[Implement an analysis step](docs/2_implement_an_analysis_step/README.md)**
3. **[Examples](examples/)**: two complete workflows, [focus](examples/focus/README.md) and [object analysis](examples/object_analysis/README.md), to read and copy from.

<sub><sub><br></sub></sub>

## Install It

Install the engine into your Python environment (Python 3.12 or newer, and conda for the step environments):

```bash
pip install "zmart-analysis @ git+https://github.com/ZMB-UZH/ZMART-analysis@v0.6.0rc9"
```

<sub><sub><br></sub></sub>

## Status

This is version 0.6.0rc9, a beta. The step format, the pipeline layout and the `Engine` calls may still change.

<sub><sub><br></sub></sub>

## Author

Thom de Hoog, Center for Microscopy and Image Analysis (ZMB), University of Zurich.
Questions and problems: please open an [issue](https://github.com/ZMB-UZH/ZMART-analysis/issues).

<sub><sub><br></sub></sub>

## License

MIT License. See [LICENSE](LICENSE) for details.

If the code in this repository inspires you, or you use it or build on it, please acknowledge it. The [CITATION.cff](CITATION.cff) file says how to cite it.
