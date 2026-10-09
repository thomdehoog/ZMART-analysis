# ZMART Analysis

[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![tests](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/thomdehoog/ZMART-analysis/actions/workflows/test.yml)
[![status](https://img.shields.io/badge/status-release%20candidate-orange)](#status)

<br>

<table>
<tr>
<td width="170"><img src="docs/zmart-analysis-icon.png" width="150" alt="ZMART Analysis"></td>
<td valign="middle">

**ZMART Analysis** analyses images while the microscope acquires them, so the results can decide what to image next.

It is part of [**ZMART**](https://github.com/thomdehoog/ZMART-microscopy) (ZMB's Microscopy-Agnostic Research Toolkit), the tools we are building for smart microscopy at the Center for Microscopy and Image Analysis (ZMB), University of Zurich.

</td>
</tr>
</table>

<br>

## The Problem

When you analyse images during an acquisition, you likely run into four problems:

1. **The tools you need do not install together.**

2. **The analysis is hard to reproduce and share.**

3. **The microscope waits for the answer.**

4. **Some analysis needs a whole well or plate, not one image.**

<br>

## The Solution

1. **Each step runs in its own conda environment.** The engine chains the steps into one pipeline.

2. **A pipeline is a YAML file, the recipe.** It lists the steps, their order and their parameters.
   Every result also records the environment and package versions each step ran with.

3. **Workers stay running.** Each image is analysed the moment it arrives, and a loaded model stays loaded.

4. **A step can wait for a scope.** It runs once a well, a plate or the whole experiment is complete.

<br>

## Want to give it a try?

```python
from zmart_analysis import Engine

engine = Engine()
engine.register("focus", "workflows/focus/pipelines/focus.yaml")
engine.submit("focus", {"image_path": "stack.tiff", "z_um": [0, 2, 4, 6, 8]})
print(engine.results("focus"))
engine.shutdown()
```

1. **[Use the engine](docs/1_use_the_engine/README.md).** Every call, the recipe and what comes back.
2. **[Implement an analysis step](docs/2_implement_an_analysis_step/README.md).** How to write a step of your own.
3. **[The workflows we use](docs/3_workflows_we_use/README.md).** Focus, object analysis and driver configuration.

## Install it

You need Python 3.11 or newer and conda:

```bash
git clone https://github.com/thomdehoog/ZMART-analysis.git
cd ZMART-analysis
pip install -e .
python workflows/focus/environments/setup_env.py   # once for each workflow you use
```

## Status

This is a release candidate for version 1.0. Small changes may still happen before the release.

## Author
Thom de Hoog, Center for Microscopy and Image Analysis (ZMB), University of
Zurich (thom.dehoog@zmb.uzh.ch, thomdehoog@gmail.com).

## License
MIT License. See LICENSE file for details.

If the code in this repository inspires you, or you use it or build on it, please acknowledge it.
The [CITATION.cff](CITATION.cff) file says how to cite it.

## Links

- [ZMART Microscopy](https://github.com/thomdehoog/ZMART-microscopy): the main repository, with the acquisition workflows
- [ZMART Controller](https://github.com/thomdehoog/ZMART-controller): one vocabulary for driving any microscope
- [ZMART drivers](https://github.com/thomdehoog/ZMART-drivers): the drivers that plug into the controller, one per microscope
- [ZMART viewer](https://github.com/thomdehoog/ZMART-viewer): the viewer
- [ZMART AI agent](https://github.com/thomdehoog/ZMART-ai-agent): drive any microscope by chatting
- [Center for Microscopy and Image Analysis (ZMB)](https://www.zmb.uzh.ch), University of Zurich
