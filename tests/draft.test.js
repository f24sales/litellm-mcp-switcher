import test from 'node:test';
import assert from 'node:assert/strict';
import {Draft} from '../src/mcp_switcher/static/state.js';
const state = (revision, servers) => ({revision, active:{servers}, ceiling:{servers}});
test('in-flight refresh cannot overwrite a local edit', () => {
  const draft = new Draft(); draft.accept(state('old', {a:null}), 0);
  const captured = draft.epoch;
  draft.edit({servers:{b:null}});
  assert.equal(draft.accept(state('old', {a:null}), captured), false);
  assert.deepEqual(draft.selection, {servers:{b:null}});
});
test('external updates preserve draft and report conflict', () => {
  const draft = new Draft(); draft.accept(state('old', {a:null}), 0);
  draft.edit({servers:{b:null}});
  draft.accept(state('new', {c:null}), draft.epoch);
  assert.equal(draft.conflict, true);
  assert.deepEqual(draft.selection, {servers:{b:null}});
  draft.reset(state('new', {c:null}));
  assert.equal(draft.conflict, false); assert.equal(draft.dirty, false);
  assert.deepEqual(draft.selection, {servers:{c:null}});
});
