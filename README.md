# ZMART Analysis

[![tests](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![status](https://img.shields.io/badge/status-release%20candidate-orange)](#status)

<table>
<tr>
<td width="170"><img src="docs/zmart-analysis-icon.png" width="150" alt="ZMART Analysis"></td>
<td valign="middle">

**ZMART Analysis** provides an image analysis engine built for smart microscopy. It allows you to trigger and scale image analysis when the data comes in, and handles conflicting dependencies, documentation and reproducibility.

It is part of [**ZMART**](https://github.com/thomdehoog/ZMART-microscopy) (ZMB's Microscopy-Agnostic Research Toolkit), the tools we use for smart microscopy at the Center for Microscopy and Image Analysis (ZMB), University of Zurich.

</td>
</tr>
</table>

## The Problem

When building smart microscopy workflows, you are likely to run into the
following four problems.

1. **Analysis over scopes.**<br>
   Depending on the experiment, analysis needs to be
   done over single images, a group of images, a compartment, a carrier, or a
   whole experiment. However, the data does not come in all at once during smart microscopy. A step
   over a scope larger than a single image can only start once the right set
   of images is in. Keeping track of all these analysis requirements calls
   for a more sophisticated degree of workflow orchestration.

2. **Reproducibility.**<br>
   Image analysis workflows need to be properly documented, reproducible and
   easily sharable. This is already a challenge in post-acquisition image
   analysis, and is even more tricky for smart microscopy workflows.

3. **Dependency conflicts.**<br>
   Analysis pipelines consist of multiple steps. Each step needs specific
   dependencies. These dependencies often conflict in
   the same environment they are executed in. This creates a real barrier for changing and getting your
   analysis up and running quickly.

4. **Time.**<br>
   The microscope waits for feedback to move on, so the analysis needs to be
   finished as soon as possible, even though computational resources are
   often limited.

## The Solution

I created the ZMART Analysis pipeline engine to address these barriers. It is
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

## Want to give it a try?

1. **[Use the engine](docs/1_use_the_engine/README.md)**
2. **[Implement an analysis step](docs/2_implement_an_analysis_step/README.md)**
3. **[The workflows we use](docs/3_workflows_we_use/README.md)**

## Install it

Python 3.11 or newer and conda are needed. The engine itself needs only
PyYAML; each workflow's `setup_env.py` makes the conda environment its
steps run in.

```bash
git clone https://github.com/thomdehoog/ZMART-analysis.git
cd ZMART-analysis
pip install -e .
python workflows/alignment/environments/setup_env.py   # once for each workflow you use
```

The package installs as `zmart_analysis`: `from zmart_analysis import Engine`.
Its old name, `engine`, still imports for the tools that use it. The
tutorials are notebooks; `pip install jupyter` to run them.

## Status

This is a release candidate. The step format, the pipeline layout and the
`Engine` calls are settled in spirit, and small changes may still happen
before 1.0. If you build a workflow on it, please open an issue so we can
keep the contract honest together.

## Author

Thom de Hoog, Center for Microscopy and Image Analysis (ZMB), University of
Zurich (thom.dehoog@zmb.uzh.ch, thomdehoog@gmail.com).

## License

MIT License. See [LICENSE](LICENSE) for details.

If the code in this repository inspires you, or you use it or build on it, please acknowledge it. The [CITATION.cff](CITATION.cff) file says how to cite it.
