import type { EffectRpc } from "../clients/rpc.ts";
import { wireValidator } from "../clients/wire-schemas.ts";
import { validatedSnapshot } from "../context/json-snapshot.ts";
import { SCHEMA } from "../generated/rsi.ts";
import type { JsonObject, PrivateRecord, RecordPage, RecordWrite, SourceFile } from "../generated/rsi.ts";
import { canonicalKey } from "../run/scoring/canonical.ts";
import { readMeasurement } from "./necessity.ts";

export function readRsi<T>(value: unknown, definition: string): T {
  return validatedSnapshot(value, wireValidator<T>(SCHEMA.$id, definition), `RSI ${definition}`);
}

export function jsonObject(value: unknown): JsonObject {
  return readRsi<JsonObject>(value, "JsonObject");
}

export function sourceFiles(value: unknown): readonly SourceFile[] {
  if (!Array.isArray(value) || value.length > 256) {
    throw new Error("RSI source requires a bounded file list");
  }
  const files = value.map((item) => readRsi<SourceFile>(item, "SourceFile"));
  if (new Set(files.map((item) => item.path)).size !== files.length) {
    throw new Error("RSI source repeats a path");
  }
  return files;
}

export function text(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0 || value.length > 512) {
    throw new Error(`Invalid RSI ${label}`);
  }
  return value;
}

export function stringList(value: unknown, label: string): readonly string[] {
  if (!Array.isArray(value) || value.length > 256) {
    throw new Error(`Invalid RSI ${label}`);
  }
  return value.map((item) => text(item, label));
}

/** Incremental O(E) replay, bounded by 65,536 records and 128 MiB per task. */
export class Records {
  readonly rpc: EffectRpc;
  sequence = 0;
  private readonly byId = new Map<string, PrivateRecord>();
  private readonly byKind = new Map<PrivateRecord["kind"], PrivateRecord[]>();
  private readonly byExperiment = new Map<string, Map<PrivateRecord["kind"], PrivateRecord[]>>();
  private readonly unsettled = new Map<string, PrivateRecord>();
  private readonly measurementIds = new Set<string>();
  private readonly sampleIds = new Set<string>();
  private bytes = 0;

  constructor(rpc: EffectRpc) {
    this.rpc = rpc;
  }

  async refresh(): Promise<void> {
    for (let pageIndex = 0; pageIndex < 1024; pageIndex += 1) {
      const cursor = this.sequence;
      const page = readRsi<RecordPage>(await this.rpc.call("rsi_list", { cursor, limit: 64 }), "RecordPage");
      for (const record of page.records) this.adopt(record);
      if (page.next_cursor === null) {
        if (page.sequence !== this.sequence) throw new Error("RSI replay omitted original records");
        return;
      }
      if (page.next_cursor !== this.sequence || this.sequence <= cursor || page.sequence < this.sequence) {
        throw new Error("RSI replay cursor failed to advance in original order");
      }
    }
    throw new Error("RSI replay exceeds the bounded page count");
  }

  get(identity: string): PrivateRecord | undefined {
    return this.byId.get(identity);
  }

  all(kind: PrivateRecord["kind"]): readonly PrivateRecord[] {
    return this.byKind.get(kind) ?? [];
  }

  last(kind: PrivateRecord["kind"]): PrivateRecord | undefined {
    return this.byKind.get(kind)?.at(-1);
  }

  baseline(): PrivateRecord {
    const branch = this.last("branch");
    const acceptance = this.last("acceptance");
    if (branch && (!acceptance || branch.sequence > acceptance.sequence)) {
      const baseline = this.get(text(branch.value.baseline_record_id, "branch baseline"));
      if (!baseline || !["initial", "acceptance"].includes(baseline.kind) || baseline.sequence >= branch.sequence) {
        throw new Error("RSI branch baseline must reference an earlier accepted source");
      }
      return baseline;
    }
    const baseline = acceptance ?? this.last("initial");
    if (!baseline) throw new Error("RSI accepted source is not initialized");
    return baseline;
  }

  experiment(kind: PrivateRecord["kind"], identity: string): readonly PrivateRecord[] {
    return this.byExperiment.get(identity)?.get(kind) ?? [];
  }

  pending(): readonly PrivateRecord[] {
    return [...this.unsettled.values()];
  }

  hasMeasurement(identity: string): boolean {
    return this.measurementIds.has(identity);
  }

  hasSample(identity: string): boolean {
    return this.sampleIds.has(identity);
  }

  request(identity: string, kind: PrivateRecord["kind"], value: unknown): RecordWrite {
    return { record_id: identity, expected_sequence: this.sequence, kind, value: jsonObject(value) };
  }

  async write(identity: string, kind: PrivateRecord["kind"], value: unknown): Promise<PrivateRecord> {
    const request = this.request(identity, kind, value);
    const existing = this.get(identity);
    if (existing) {
      if (existing.kind !== kind || canonicalKey(existing.value) !== canonicalKey(request.value)) {
        throw new Error(`RSI record identity reused with changed content: ${identity}`);
      }
      return existing;
    }
    const result = readRsi<PrivateRecord>(await this.rpc.call("rsi_record", { ...request }), "PrivateRecord");
    this.checkWrite(result, request);
    this.adopt(result);
    return result;
  }

  checkWrite(result: PrivateRecord, request: RecordWrite): void {
    if (result.record_id !== request.record_id || result.kind !== request.kind
        || canonicalKey(result.value) !== canonicalKey(request.value)) {
      throw new Error("RSI original record differs from its submitted intent");
    }
  }

  adopt(record: PrivateRecord): void {
    const existing = this.get(record.record_id);
    if (existing) {
      if (canonicalKey(existing) !== canonicalKey(record)) throw new Error("RSI original record changed on replay");
      return;
    }
    if (record.sequence !== this.sequence + 1) throw new Error("RSI records are not in original sequence order");
    const bytes = Buffer.byteLength(canonicalKey(record));
    if (this.byId.size >= 65536 || this.bytes + bytes > 128 * 1024 * 1024) {
      throw new Error("RSI replay exceeds its retained memory bound");
    }
    this.indexMeasurement(record);
    this.bytes += bytes;
    this.sequence = record.sequence;
    this.byId.set(record.record_id, record);
    const group = this.byKind.get(record.kind) ?? [];
    group.push(record);
    this.byKind.set(record.kind, group);
    const experiment = record.value.experiment_id;
    if (typeof experiment === "string") {
      const kinds = this.byExperiment.get(experiment) ?? new Map();
      const entries = kinds.get(record.kind) ?? [];
      entries.push(record);
      kinds.set(record.kind, entries);
      this.byExperiment.set(experiment, kinds);
      if (record.kind === "proposal") this.unsettled.set(experiment, record);
      if (record.kind === "acceptance") this.unsettled.delete(experiment);
    }
  }

  private indexMeasurement(record: PrivateRecord): void {
    if (record.kind !== "measurement") return;
    const measurement = readMeasurement(record.value.measurement);
    if (this.hasMeasurement(measurement.measurement_id) || measurement.sample_ids.some((identity) => this.hasSample(identity))) {
      throw new Error("RSI original history repeats a measurement or sample identity");
    }
    this.measurementIds.add(measurement.measurement_id);
    for (const identity of measurement.sample_ids) this.sampleIds.add(identity);
  }
}
