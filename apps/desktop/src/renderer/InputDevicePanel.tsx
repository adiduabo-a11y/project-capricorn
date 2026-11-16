import type { InputDeviceController } from './audio/useInputDevice'

/**
 * The microphone the whole renderer listens through.
 *
 * Unlike `config.yaml`, a device id belongs to this PC, so the choice is stored
 * here (like the mic route) and applied to the shared capture graph.
 */
export function InputDevicePanel({ device }: { device: InputDeviceController }) {
  const missing = device.deviceId !== '' && !device.mics.some((mic) => mic.id === device.deviceId)

  return (
    <section className="input-device" aria-label="Voice microphone">
      <div className="input-device-head">
        <span className="input-device-title">Voice microphone</span>
        <button className="mem-sweep" type="button" onClick={() => void device.refresh()}>
          refresh
        </button>
      </div>
      <label className="set-field">
        <span className="set-label">Input device</span>
        <select
          className="set-input"
          value={device.deviceId}
          onChange={(event) => device.choose(event.target.value)}
        >
          <option value="">System default</option>
          {device.mics.map((mic) => (
            <option key={mic.id} value={mic.id}>
              {mic.label}
            </option>
          ))}
          {missing ? (
            <option value={device.deviceId}>Saved microphone (not connected)</option>
          ) : null}
        </select>
      </label>
      <span className="set-help">
        One stream feeds push-to-talk, barge-in and her name. Stored on this PC.
      </span>
      {device.note ? <p className="set-error">{device.note}</p> : null}
    </section>
  )
}
