// A generation counter protects edits from requests that were already in flight.
export class Draft {
  constructor() { this.epoch = 0; this.dirty = false; this.conflict = false; this.revision = null; this.selection = {servers: {}}; }
  edit(selection) { this.selection = structuredClone(selection); this.dirty = true; this.epoch++; }
  accept(state, capturedEpoch, admin = false) {
    if (this.dirty || this.epoch !== capturedEpoch) {
      if (this.revision && this.revision !== state.revision) this.conflict = true;
      return false;
    }
    this.revision = state.revision;
    this.selection = structuredClone(admin ? state.ceiling : state.active);
    this.conflict = false;
    return true;
  }
  reset(state, admin = false) { this.dirty = false; this.epoch++; return this.accept(state, this.epoch, admin); }
}
