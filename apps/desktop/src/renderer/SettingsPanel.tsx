import { useCallback, useEffect, useState } from 'react'

import { CalibrateVoice } from './CalibrateVoice'
import { InputDevicePanel } from './InputDevicePanel'
import { McpPanel } from './McpPanel'
import { MicRoutePanel } from './MicRoutePanel'
import type { InputDeviceController } from './audio/useInputDevice'
import type { MicRouteController } from './audio/useMicRoute'
import { capri } from './bridge'

/** The subset of JSON Schema Pydantic emits that this form understands. */
interface JsonSchema {
  type?: string | string[]
  title?: string
  description?: string
  properties?: Record<string, JsonSchema>
  required?: string[]
  $ref?: string
  $defs?: Record<string, JsonSchema>
  anyOf?: JsonSchema[]
  enum?: Array<string | number | boolean | null>
  items?: JsonSchema
  minimum?: number
  maximum?: number
}

type Config = Record<string, unknown>

interface ModelsStatus {
  endpoint: string
  online: boolean
  model: string
  structured: string
  embed?: string
  embedDim?: number
  embedMode?: string
}

interface Props {
  open: boolean
  online: boolean
  micRoute: MicRouteController
  inputDevice: InputDeviceController
  /** Calibration holds the microphone: the wake gate has to let go. */
  onCalibrateBusy: (busy: boolean) => void
  /** New numbers were saved; the gate reopens with them. */
  onCalibrated: () => void
  onClose: () => void
}

function deref(
  schema: JsonSchema,
  root: JsonSchema,
): { schema: JsonSchema; nullable: boolean } {
  let current = schema
  let nullable = false
  for (let guard = 0; guard < 12; guard += 1) {
    if (current.$ref) {
      const name = current.$ref.replace(/^#\/\$defs\//, '')
      const next = root.$defs?.[name]
      if (!next) break
      current = next
      continue
    }
    if (current.anyOf) {
      const nonNull = current.anyOf.find((item) => item.type !== 'null')
      if (!nonNull) break
      nullable = nullable || current.anyOf.some((item) => item.type === 'null')
      current = nonNull
      continue
    }
    break
  }
  return { schema: current, nullable }
}

function valueAt(config: Config, path: string[]): unknown {
  let node: unknown = config
  for (const part of path) {
    if (node === null || typeof node !== 'object') return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

function wakeNumber(config: Config, key: string): number | undefined {
  const wake = valueAt(config, ['voice', 'wake'])
  const value = wake && typeof wake === 'object' ? (wake as Record<string, unknown>)[key] : undefined
  return typeof value === 'number' ? value : undefined
}

function labelFor(key: string, schema: JsonSchema): string {
  return schema.title || key.replace(/_/g, ' ')
}

function DraftInput({
  schema,
  value,
  nullable,
  disabled,
  placeholder,
  onSave,
}: {
  schema: JsonSchema
  value: unknown
  nullable: boolean
  disabled: boolean
  placeholder?: string
  onSave: (value: unknown) => void
}) {
  const [draft, setDraft] = useState(value == null ? '' : String(value))

  useEffect(() => {
    setDraft(value == null ? '' : String(value))
  }, [value])

  const numeric = schema.type === 'number' || schema.type === 'integer'
  const commit = () => {
    if (numeric) {
      if (draft.trim() === '') return
      const number = Number(draft)
      if (!Number.isFinite(number)) return
      if (number !== value) onSave(number)
      return
    }
    const next = nullable && draft === '' ? null : draft
    if (next !== (value ?? null)) onSave(next)
  }

  return (
    <input
      className="set-input"
      type={numeric ? 'number' : 'text'}
      value={draft}
      placeholder={placeholder}
      min={schema.minimum}
      max={schema.maximum}
      step={schema.type === 'integer' ? 1 : 'any'}
      disabled={disabled}
      spellCheck={false}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === 'Enter') event.currentTarget.blur()
      }}
    />
  )
}

function ArrayInput({
  value,
  disabled,
  onSave,
}: {
  value: unknown
  disabled: boolean
  onSave: (value: unknown) => void
}) {
  const [draft, setDraft] = useState(JSON.stringify(value ?? [], null, 2))
  const [invalid, setInvalid] = useState(false)

  useEffect(() => {
    setDraft(JSON.stringify(value ?? [], null, 2))
    setInvalid(false)
  }, [value])

  const commit = () => {
    try {
      const parsed = JSON.parse(draft)
      if (!Array.isArray(parsed)) throw new Error('not an array')
      setInvalid(false)
      if (JSON.stringify(parsed) !== JSON.stringify(value)) onSave(parsed)
    } catch {
      setInvalid(true)
    }
  }

  return (
    <div className="set-array">
      <textarea
        value={draft}
        disabled={disabled}
        spellCheck={false}
        rows={3}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
      />
      {invalid ? <span className="set-error">must be a JSON array</span> : null}
    </div>
  )
}

function Field({
  name,
  path,
  schema,
  root,
  config,
  depth,
  saving,
  onSave,
}: {
  name: string
  path: string[]
  schema: JsonSchema
  root: JsonSchema
  config: Config
  depth: number
  saving: string | null
  onSave: (path: string[], value: unknown) => void
}) {
  const resolved = deref(schema, root)
  const shape = resolved.schema
  const value = valueAt(config, path)
  const dotted = path.join('.')
  const disabled = saving !== null
  const busy = saving === dotted

  if (shape.properties) {
    return (
      <details className={`set-object ${depth === 0 ? 'set-section' : ''}`}>
        <summary>
          <span>{labelFor(name, shape)}</span>
          {shape.description ? <small>{shape.description}</small> : null}
        </summary>
        <div className="set-fields">
          {Object.entries(shape.properties).map(([key, child]) => (
            <Field
              key={key}
              name={key}
              path={[...path, key]}
              schema={child}
              root={root}
              config={config}
              depth={depth + 1}
              saving={saving}
              onSave={onSave}
            />
          ))}
        </div>
      </details>
    )
  }

  if (shape.type === 'array' || shape.items) {
    return (
      <label className="set-field set-field-wide">
        <span className="set-label">{labelFor(name, shape)}</span>
        {shape.description ? <span className="set-help">{shape.description}</span> : null}
        <ArrayInput value={value} disabled={disabled} onSave={(next) => onSave(path, next)} />
      </label>
    )
  }

  if (shape.enum) {
    return (
      <label className="set-field">
        <span className="set-label">{labelFor(name, shape)}</span>
        {shape.description ? <span className="set-help">{shape.description}</span> : null}
        <select
          className="set-input"
          value={value == null ? '' : String(value)}
          disabled={disabled}
          onChange={(event) => {
            const choice =
              event.target.value === ''
                ? null
                : shape.enum?.find((item) => String(item) === event.target.value)
            onSave(path, choice ?? null)
          }}
        >
          {resolved.nullable ? <option value="">(unset)</option> : null}
          {shape.enum.map((item) => (
            <option key={String(item)} value={String(item)}>
              {String(item)}
            </option>
          ))}
        </select>
        {busy ? <span className="set-saving">saving…</span> : null}
      </label>
    )
  }

  if (shape.type === 'boolean') {
    return (
      <label className="set-field set-toggle">
        <span>
          <span className="set-label">{labelFor(name, shape)}</span>
          {shape.description ? <span className="set-help">{shape.description}</span> : null}
        </span>
        <input
          type="checkbox"
          checked={Boolean(value)}
          disabled={disabled}
          onChange={(event) => onSave(path, event.target.checked)}
        />
        {busy ? <span className="set-saving">saving…</span> : null}
      </label>
    )
  }

  if (shape.type === 'string' || shape.type === 'number' || shape.type === 'integer') {
    return (
      <label className="set-field">
        <span className="set-label">{labelFor(name, shape)}</span>
        {shape.description ? <span className="set-help">{shape.description}</span> : null}
        <DraftInput
          schema={shape}
          value={value}
          nullable={resolved.nullable}
          disabled={disabled}
          onSave={(next) => onSave(path, next)}
        />
        {busy ? <span className="set-saving">saving…</span> : null}
      </label>
    )
  }

  return null
}

function BrainPanel({
  config,
  modelSchema,
  root,
  saving,
  onSave,
}: {
  config: Config
  modelSchema: JsonSchema
  root: JsonSchema
  saving: string | null
  onSave: (path: string[], value: unknown) => void
}) {
  const [status, setStatus] = useState<ModelsStatus | null>(null)
  const [checking, setChecking] = useState(false)
  const [checkError, setCheckError] = useState('')

  const check = useCallback(async () => {
    setChecking(true)
    setCheckError('')
    try {
      setStatus(await capri.call<ModelsStatus>('models.status'))
    } catch (err) {
      setStatus(null)
      setCheckError(err instanceof Error ? err.message : String(err))
    } finally {
      setChecking(false)
    }
  }, [])

  useEffect(() => {
    void check()
  }, [check])

  const prop = (key: string): JsonSchema => modelSchema.properties?.[key] ?? {}
  const endpoint = valueAt(config, ['models', 'endpoint'])
  const model = valueAt(config, ['models', 'model'])
  const structured = String(valueAt(config, ['models', 'structured']) ?? 'json_schema')
  const disabled = saving !== null
  const configured = String(endpoint ?? '').trim().length > 0
  const state = !configured ? 'idle' : checking ? 'wait' : status?.online ? 'on' : status ? 'off' : 'idle'
  const statusText = !configured
    ? 'not configured'
    : checking
      ? 'checking server…'
      : status?.online
        ? 'server reachable'
        : status
          ? 'server not reachable'
          : 'not checked'

  return (
    <section className="brain-panel">
      <div className="brain-head">
        <span className="set-title">Brain</span>
        <span className="set-sub">one local model · LM Studio or llama.cpp</span>
        <span className="spacer" />
        <span className="brain-badge" data-state={state}>
          {statusText}
        </span>
      </div>

      <p className="set-help">
        Start LM Studio’s local server or <strong>llama-server</strong>, then
        paste the OpenAI-compatible base URL here. Capri uses this one model for
        planning, replies, and background work; choose the model in the server,
        not in a preset.
      </p>

      <div className="set-fields">
        <div className="set-field-wide">
          <label className="set-field">
            <span className="set-label">Server URL</span>
            <span className="set-help">
              {prop('endpoint').description ??
                'OpenAI-compatible base URL, including /v1.'}
            </span>
            <DraftInput
              schema={prop('endpoint')}
              value={endpoint}
              nullable={false}
              disabled={disabled}
              placeholder="http://127.0.0.1:1234/v1"
              onSave={(next) => onSave(['models', 'endpoint'], next)}
            />
          </label>
          {saving === 'models.endpoint' ? (
            <span className="set-saving">saving…</span>
          ) : null}
        </div>

        <div className="set-field-wide">
          <label className="set-field">
            <span className="set-label">Model ID</span>
            <span className="set-help">
              {prop('model').description ??
                'The model identifier shown by your server at /v1/models.'}
            </span>
            <DraftInput
              schema={prop('model')}
              value={model}
              nullable={false}
              disabled={disabled}
              placeholder="model id from /v1/models"
              onSave={(next) => onSave(['models', 'model'], next)}
            />
          </label>
          {saving === 'models.model' ? (
            <span className="set-saving">saving…</span>
          ) : null}
        </div>

        <label className="set-field">
          <span className="set-label">Structured output</span>
          <span className="set-help">
            JSON schema is the default; use GBNF if your llama.cpp server rejects it.
          </span>
          <select
            className="set-input"
            value={structured}
            disabled={disabled}
            onChange={(event) => onSave(['models', 'structured'], event.target.value)}
          >
            <option value="json_schema">JSON schema — LM Studio (default)</option>
            <option value="grammar">GBNF grammar — llama.cpp fallback</option>
            <option value="none">Prompt only</option>
          </select>
          {saving === 'models.structured' ? (
            <span className="set-saving">saving…</span>
          ) : null}
        </label>

        <div className="brain-check">
          <button
            className="mem-sweep"
            type="button"
            disabled={checking || disabled}
            onClick={() => void check()}
          >
            {checking ? 'checking…' : 'test server'}
          </button>
          <span className="set-help">
            Tests the saved URL. Press Enter or leave a field to save it first.
          </span>
        </div>
      </div>

      {checkError ? <p className="set-error">{checkError}</p> : null}
      {status?.online ? (
        <p className="brain-result">
          Answered at {status.endpoint || 'the configured URL'}
          {status.model ? ` · ${status.model}` : ' · no model ID selected'}
        </p>
      ) : null}

      <div className="brain-tip">
        <span>
          <strong>LM Studio</strong> — Developer → Local Server, start it, then
          use the URL it shows (usually http://127.0.0.1:1234/v1).
        </span>
        <span>
          <strong>llama.cpp</strong> — run llama-server, then use its base URL
          (usually http://127.0.0.1:8080/v1).
        </span>
      </div>

      <details className="set-object brain-advanced">
        <summary>
          <span>Advanced</span>
          <small>sampling and timeouts</small>
        </summary>
        <div className="set-fields">
          <Field
            name="temperature"
            path={['models', 'temperature']}
            schema={prop('temperature')}
            root={root}
            config={config}
            depth={1}
            saving={saving}
            onSave={onSave}
          />
          <Field
            name="disable_thinking"
            path={['models', 'disable_thinking']}
            schema={prop('disable_thinking')}
            root={root}
            config={config}
            depth={1}
            saving={saving}
            onSave={onSave}
          />
          <Field
            name="request_timeout_s"
            path={['models', 'request_timeout_s']}
            schema={prop('request_timeout_s')}
            root={root}
            config={config}
            depth={1}
            saving={saving}
            onSave={onSave}
          />
          <Field
            name="health_ttl_s"
            path={['models', 'health_ttl_s']}
            schema={prop('health_ttl_s')}
            root={root}
            config={config}
            depth={1}
            saving={saving}
            onSave={onSave}
          />
        </div>
      </details>

      <details className="set-object brain-advanced">
        <summary>
          <span>Retrieval models</span>
          <small>optional · memory and documents</small>
        </summary>
        <div className="set-fields">
          <Field
            name="embed"
            path={['models', 'embed']}
            schema={prop('embed')}
            root={root}
            config={config}
            depth={1}
            saving={saving}
            onSave={onSave}
          />
          <Field
            name="rerank"
            path={['models', 'rerank']}
            schema={prop('rerank')}
            root={root}
            config={config}
            depth={1}
            saving={saving}
            onSave={onSave}
          />
        </div>
      </details>
    </section>
  )
}

/**
 * A form generated from `config.schema`. Every control writes one dotted path
 * through `config.set`, and the daemon's validated config replaces the local
 * copy. Brain is hand-laid-out because choosing a server and model is the one
 * thing every other setting depends on; the rest is generated from Pydantic.
 */
export function SettingsPanel({
  open,
  online,
  micRoute,
  inputDevice,
  onCalibrateBusy,
  onCalibrated,
  onClose,
}: Props) {
  const [config, setConfig] = useState<Config | null>(null)
  const [schema, setSchema] = useState<JsonSchema | null>(null)
  const [saving, setSaving] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open || !online) return
    let alive = true
    setLoading(true)
    setNote('')
    Promise.all([
      capri.call<Config>('config.get'),
      capri.call<JsonSchema>('config.schema'),
    ])
      .then(([nextConfig, nextSchema]) => {
        if (!alive) return
        setConfig(nextConfig)
        setSchema(nextSchema)
      })
      .catch((err) => {
        if (alive) setNote(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [online, open])

  const save = useCallback(async (path: string[], value: unknown) => {
    const dotted = path.join('.')
    setSaving(dotted)
    setNote('')
    try {
      const updated = await capri.call<Config>('config.set', { path: dotted, value })
      setConfig(updated)
      setNote(`saved ${dotted}`)
    } catch (err) {
      setNote(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(null)
    }
  }, [])

  if (!open) return null

  const modelSchema =
    schema && schema.properties?.models
      ? deref(schema.properties.models, schema).schema
      : null

  return (
    <div className="settings">
      <div className="set-head">
        <span className="set-title">Settings</span>
        <span className="set-sub">brain · voice · behavior</span>
        <span className="spacer" />
        <button className="mem-sweep" onClick={onClose}>
          close
        </button>
      </div>
      {config && schema && modelSchema ? (
        <BrainPanel
          config={config}
          modelSchema={modelSchema}
          root={schema}
          saving={saving}
          onSave={save}
        />
      ) : null}
      <McpPanel online={online} />
      <InputDevicePanel device={inputDevice} />
      <MicRoutePanel route={micRoute} />
      {config ? (
        <CalibrateVoice
          thresholdRms={wakeNumber(config, 'threshold_rms')}
          minUtteranceMs={wakeNumber(config, 'min_utterance_ms')}
          onSave={save}
          onBusy={onCalibrateBusy}
          onApplied={onCalibrated}
        />
      ) : null}
      {note ? <p className="mem-note">{note}</p> : null}
      {loading ? <p className="empty">reading config…</p> : null}
      {!online ? <p className="empty">Settings need the core online.</p> : null}
      {config && schema ? (
        <div className="set-body">
          {Object.entries(schema.properties ?? {})
            .filter(([key]) => key !== 'models' && key !== 'mcp')
            .map(([key, child]) => (
              <Field
                key={key}
                name={key}
                path={[key]}
                schema={child}
                root={schema}
                config={config}
                depth={0}
                saving={saving}
                onSave={save}
              />
            ))}
        </div>
      ) : null}
    </div>
  )
}
