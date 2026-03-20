import { useState, useEffect, useMemo, useCallback } from 'react'
import Plotly from 'plotly.js-dist-min'
import plotFactory from 'react-plotly.js/factory.js'
import './App.css'
import pipelineData from './data.json'

const createPlot = plotFactory.default || plotFactory
const Plot = createPlot(Plotly)

const FEATURES = [
  { key: 'area', label: 'Area (px)' },
  { key: 'eccentricity', label: 'Eccentricity' },
  { key: 'meanIntensity', label: 'Mean Intensity' },
  { key: 'maxIntensity', label: 'Max Intensity' },
  { key: 'solidity', label: 'Solidity' },
  { key: 'majorAxis', label: 'Major Axis' },
  { key: 'minorAxis', label: 'Minor Axis' },
]

const AMBER = '#f59e0b'
const MUTED = '#475569'

function RangeFilter({ label, min, max, value, onChange }) {
  const [lo, hi] = value
  const pct = (v) => ((v - min) / (max - min)) * 100

  return (
    <div className="filter-group">
      <div className="filter-label">
        <span>{label}</span>
        <span className="filter-range">
          {lo.toFixed(lo < 1 ? 3 : 0)} — {hi.toFixed(hi < 1 ? 3 : 0)}
        </span>
      </div>
      <div className="range-slider">
        <div className="range-slider-track">
          <div
            className="range-slider-fill"
            style={{ left: `${pct(lo)}%`, width: `${pct(hi) - pct(lo)}%` }}
          />
        </div>
        <div className="range-slider-inputs">
          <input
            type="range"
            min={min}
            max={max}
            step={(max - min) / 200}
            value={lo}
            onChange={(e) => onChange([Math.min(+e.target.value, hi), hi])}
          />
          <input
            type="range"
            min={min}
            max={max}
            step={(max - min) / 200}
            value={hi}
            onChange={(e) => onChange([lo, Math.max(+e.target.value, lo)])}
          />
        </div>
      </div>
    </div>
  )
}

function CellTable({ cells, sortCol, sortDir, onSort }) {
  const cols = [
    { key: 'label', label: 'Label' },
    { key: 'area', label: 'Area' },
    { key: 'cx', label: 'X' },
    { key: 'cy', label: 'Y' },
    { key: 'eccentricity', label: 'Ecc.' },
    { key: 'meanIntensity', label: 'Intensity' },
    { key: 'solidity', label: 'Solidity' },
    { key: 'majorAxis', label: 'Major' },
    { key: 'minorAxis', label: 'Minor' },
  ]

  const fmt = (key, v) => {
    if (key === 'label' || key === 'area') return v
    if (key === 'eccentricity' || key === 'solidity') return v.toFixed(3)
    return v.toFixed(1)
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {cols.map(({ key, label }) => (
              <th key={key} onClick={() => onSort(key)}>
                {label}
                <span className={`sort-arrow ${sortCol === key ? 'active' : ''}`}>
                  {sortCol === key ? (sortDir === 'asc' ? '\u25b2' : '\u25bc') : '\u25b4'}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {cells.map((c) => (
            <tr key={c.label}>
              {cols.map(({ key }) => (
                <td key={key}>{fmt(key, c[key])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function App() {
  const [data, setData] = useState(null)
  const [filters, setFilters] = useState({})
  const [axisX, setAxisX] = useState('area')
  const [axisY, setAxisY] = useState('meanIntensity')
  const [sortCol, setSortCol] = useState('label')
  const [sortDir, setSortDir] = useState('asc')

  // Load data
  useEffect(() => {
    const d = pipelineData
    setData(d)
    const init = {}
    FEATURES.forEach(({ key }) => {
      const vals = d.cells.map((c) => c[key])
      const lo = Math.min(...vals)
      const hi = Math.max(...vals)
      const pad = (hi - lo) * 0.02 || 0.5
      init[key] = { min: lo - pad, max: hi + pad, value: [lo - pad, hi + pad] }
    })
    setFilters(init)
  }, [])

  // Filter cells
  const { selected, unselected } = useMemo(() => {
    if (!data || !Object.keys(filters).length) return { selected: [], unselected: [] }

    const sel = []
    const unsel = []
    data.cells.forEach((c) => {
      let pass = true
      for (const { key } of FEATURES) {
        const f = filters[key]
        if (!f) continue
        if (c[key] < f.value[0] || c[key] > f.value[1]) { pass = false; break }
      }
      pass ? sel.push(c) : unsel.push(c)
    })
    return { selected: sel, unselected: unsel }
  }, [data, filters])

  // Sort selected cells
  const sortedCells = useMemo(() => {
    const arr = [...selected]
    arr.sort((a, b) => {
      const va = a[sortCol], vb = b[sortCol]
      return sortDir === 'asc' ? va - vb : vb - va
    })
    return arr
  }, [selected, sortCol, sortDir])

  const handleSort = useCallback((col) => {
    if (col === sortCol) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortCol(col)
      setSortDir('asc')
    }
  }, [sortCol])

  const handleFilterChange = useCallback((key, value) => {
    setFilters((prev) => ({ ...prev, [key]: { ...prev[key], value } }))
  }, [])

  const handleReset = useCallback(() => {
    setFilters((prev) => {
      const next = {}
      Object.entries(prev).forEach(([k, v]) => {
        next[k] = { ...v, value: [v.min, v.max] }
      })
      return next
    })
  }, [])

  if (!data) {
    return (
      <div className="loading">
        <div className="loading-spinner" />
        Loading pipeline data...
      </div>
    )
  }

  const h = data.height
  const w = data.width
  const imgSrc = `data:image/png;base64,${data.image}`

  // ── Image figure ──
  const imgFig = {
    data: [
      unselected.length > 0 && {
        type: 'scatter',
        x: unselected.map((c) => c.cx),
        y: unselected.map((c) => c.cy),
        mode: 'markers',
        marker: { size: 5, color: MUTED, opacity: 0.25 },
        hoverinfo: 'skip',
        showlegend: false,
      },
      selected.length > 0 && {
        type: 'scatter',
        x: selected.map((c) => c.cx),
        y: selected.map((c) => c.cy),
        mode: 'markers',
        marker: {
          size: 8,
          color: AMBER,
          opacity: 0.9,
          line: { width: 1.5, color: 'white' },
        },
        text: selected.map(
          (c) =>
            `<b>Cell ${c.label}</b><br>` +
            `Area: ${c.area} px<br>` +
            `Intensity: ${c.meanIntensity.toFixed(1)}<br>` +
            `Eccentricity: ${c.eccentricity.toFixed(3)}<br>` +
            `Solidity: ${c.solidity.toFixed(3)}`
        ),
        hoverinfo: 'text',
        showlegend: false,
      },
    ].filter(Boolean),
    layout: {
      images: [
        {
          source: imgSrc,
          xref: 'x',
          yref: 'y',
          x: 0,
          y: 0,
          sizex: w,
          sizey: h,
          sizing: 'stretch',
          layer: 'below',
          xanchor: 'left',
          yanchor: 'top',
        },
      ],
      xaxis: {
        range: [0, w],
        showgrid: false,
        zeroline: false,
        showticklabels: false,
        scaleanchor: 'y',
      },
      yaxis: {
        range: [h, 0],
        showgrid: false,
        zeroline: false,
        showticklabels: false,
      },
      margin: { l: 0, r: 0, t: 0, b: 0 },
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      dragmode: 'pan',
      height: 480,
    },
    config: { scrollZoom: true, displayModeBar: true, modeBarButtonsToRemove: ['toImage', 'lasso2d', 'select2d'] },
  }

  // ── Scatter figure ──
  const xLabel = FEATURES.find((f) => f.key === axisX)?.label || axisX
  const yLabel = FEATURES.find((f) => f.key === axisY)?.label || axisY

  const scatterFig = {
    data: [
      unselected.length > 0 && {
        type: 'scatter',
        x: unselected.map((c) => c[axisX]),
        y: unselected.map((c) => c[axisY]),
        mode: 'markers',
        marker: { size: 4, color: MUTED, opacity: 0.2 },
        name: 'Filtered out',
        hoverinfo: 'skip',
      },
      selected.length > 0 && {
        type: 'scatter',
        x: selected.map((c) => c[axisX]),
        y: selected.map((c) => c[axisY]),
        mode: 'markers',
        marker: {
          size: 6,
          color: AMBER,
          opacity: 0.75,
          line: { width: 0.5, color: 'white' },
        },
        name: 'Selected',
        text: selected.map((c) => `Cell ${c.label}`),
        hoverinfo: 'text',
      },
    ].filter(Boolean),
    layout: {
      xaxis: { title: { text: xLabel, font: { size: 12 } }, gridcolor: '#1e293b' },
      yaxis: { title: { text: yLabel, font: { size: 12 } }, gridcolor: '#1e293b' },
      margin: { l: 55, r: 15, t: 10, b: 50 },
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      template: 'plotly_dark',
      legend: { x: 0.01, y: 0.99, bgcolor: 'rgba(0,0,0,0.4)', font: { size: 11 } },
      height: 430,
      dragmode: 'pan',
    },
    config: { scrollZoom: true, displayModeBar: true, modeBarButtonsToRemove: ['toImage'] },
  }

  return (
    <div className="app">
      {/* Header */}
      <header className="header">
        <div className="header-left">
          <div className="header-title">
            SMART<span> Analysis</span>
          </div>
        </div>
        <div className="header-right">
          <span className="header-meta">{data.source}</span>
          <span className="header-meta">{w}&times;{h}px</span>
          <span className="badge badge-cyan">{data.nCells} cells</span>
          <span className="badge badge-amber">{selected.length} selected</span>
        </div>
      </header>

      {/* Main */}
      <div className="main">
        {/* Image */}
        <div className="card">
          <div className="card-header">Segmentation</div>
          <div className="card-body">
            <Plot
              data={imgFig.data}
              layout={imgFig.layout}
              config={imgFig.config}
              useResizeHandler
              style={{ width: '100%', height: '100%' }}
            />
          </div>
        </div>

        {/* Scatter */}
        <div className="card">
          <div className="card-header">Feature Space</div>
          <div className="card-body">
            <div className="axis-row">
              <select
                className="axis-select"
                value={axisX}
                onChange={(e) => setAxisX(e.target.value)}
              >
                {FEATURES.map(({ key, label }) => (
                  <option key={key} value={key}>{label}</option>
                ))}
              </select>
              <select
                className="axis-select"
                value={axisY}
                onChange={(e) => setAxisY(e.target.value)}
              >
                {FEATURES.map(({ key, label }) => (
                  <option key={key} value={key}>{label}</option>
                ))}
              </select>
            </div>
            <Plot
              data={scatterFig.data}
              layout={scatterFig.layout}
              config={scatterFig.config}
              useResizeHandler
              style={{ width: '100%', height: '100%' }}
            />
          </div>
        </div>

        {/* Filters */}
        <div className="card filters-card">
          <div className="card-header">
            <span>Filters</span>
            <button className="btn-reset" onClick={handleReset}>Reset All</button>
          </div>
          <div className="card-body">
            {FEATURES.map(({ key, label }) => {
              const f = filters[key]
              if (!f) return null
              return (
                <RangeFilter
                  key={key}
                  label={label}
                  min={f.min}
                  max={f.max}
                  value={f.value}
                  onChange={(v) => handleFilterChange(key, v)}
                />
              )
            })}
          </div>
        </div>

        {/* Table */}
        <div className="card table-card">
          <div className="card-header">
            <span>Selected Cells</span>
            <span style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 400, textTransform: 'none' }}>
              {selected.length} of {data.nCells}
            </span>
          </div>
          <div className="card-body" style={{ padding: 0 }}>
            <CellTable
              cells={sortedCells}
              sortCol={sortCol}
              sortDir={sortDir}
              onSort={handleSort}
            />
          </div>
        </div>
      </div>
    </div>
  )
}
