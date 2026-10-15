# ZMART Analysis

[![tests](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![status](https://img.shields.io/badge/status-release%20candidate-orange)](#status)

<table>
<tr>
<td width="170"><img src="docs/zmart-analysis-icon.png" width="150" alt="ZMART Analysis"></td>
<td valign="middle">

**ZMART Analysis** is an image analysis engine built for smart microscopy. It allows you to triggers and scale image analysis when the data comes in, handles dependencies, documentation and reproducibility. 

It is part of [**ZMART**](https://github.com/thomdehoog/ZMART-microscopy) (ZMB's Microscopy-Agnostic Research Toolkit), the tools we use for smart microscopy at the Center for Microscopy and Image Analysis (ZMB), University of Zurich.

</td>
</tr>
</table>

## The Problem

When building smart microscopy workflows, you are likely to run into the
following four problems.

1. **Analysis over scopes.** <br>Depending on the experiment, analysis needs to
   be done over single images, a group of images, a compartment, a carrier,
   or a whole experiment. However, the data does not come in all at once. A
   step over a larger scope can only start once the right set of images is
   in. Keeping track of all these analysis requirements calls for more
   sophisticated degree of workflow orchestration.

3. **Reproducibility.** <br>The complexity in workflow orchestration need to be
   documented and reproducible. This is already a challenge in post-acquisition
   workflows, and is more tricky for smart microscopy workflows.

4. **Dependency conflicts.** <br>Analysis pipelines consist of multiple steps.
   Each step needs often need specific decencies. However, these decencies
   can often not be installed together in the same virtual environment. This creates real
   barrier for getting your analysis up and running quickly  

5. **Time.** <br>The microscope waits for feedback to move one, therefore analysis
   needs to be finished as soon as possible. Even though computational resources
   are often limited.


## The Solution

I am not aware of a current tool that takes these 4 barriers away. Therefore, I created
the ZMART Analysis pipeline engine. It address these barriers in the following way, 
while aiming the minimize input, configuration as much friction as possible.

1. **Analysis steps declare a scope.** <br>Each step says over which scope its analysis
   needs to run: a single image, a group, a compartment, a carrier, or the experiment.
   An analysis step automatically is triggered as soon as the data over the
   given scope is completely acquired.

2. **Analysis pipelines are declared in YAML files.** <br>A YAML file defines
   which steps are run, in which order, and with which settings. You can reuse theme
   as templates, so it is easy to adjust and provide documentation that makes you analysis.
   
3. **Steps can be isolated and executed by different workers** <br>Each step names
   the conda environment it needs. Steps can share an environment or use separate
   ones, so dependency conflicts are avoided.
   
4. **Workers stay active and can run steps concurrently.** <br>Multiple workers
   can be initialized. They stay active and accept tasks as soon as they are
   free.


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
python workflows/focus/environments/setup_env.py   # once for each workflow you use
```

The package installs as `zmart_analysis`: `from zmart_analysis import Engine`.
Its old name, `engine`, still imports for the tools that use it. The
tutorials are notebooks; `pip install jupyter` to run them.

## Status

This is a release candidate. The step format, the recipe layout and the
`Engine` calls are settled in spirit, and small changes may still happen
before 1.0. If you build a workflow on it, please open an issue so we can
keep the contract honest together.

## Author

Thom de Hoog, Center for Microscopy and Image Analysis (ZMB), University of
Zurich (thom.dehoog@zmb.uzh.ch, thomdehoog@gmail.com).

## License

MIT License. See [LICENSE](LICENSE) for details.

If the code in this repository inspires you, or you use it or build on it, please acknowledge it. The [CITATION.cff](CITATION.cff) file says how to cite it.
