// Execute the actual Vue render function with local result data; no browser or printer connection.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let source = fs.readFileSync(path.join(__dirname, '../app/assets/screws-list-render.js.txt'), 'utf8');
const edits = JSON.parse(fs.readFileSync(path.join(__dirname, '../app/assets/screws-grid-edits.json'), 'utf8'));
for (const [before, after] of edits) {
  assert.equal(source.split(before).length - 1, 1);
  source = source.replace(before, after);
}
const sandbox = {et: 'dialog', Ce: 'content', qt: 'table', wt: 'spacer', k: 'button', mr: 'chip', x: 'icon', _n: 'progress'};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const screws = [
  {key: 'screw1', prettyName: 'Front left screw', x: 45, y: 28, z: 0, is_base: true, adjustMinutes: 0},
  {key: 'screw2', prettyName: 'Front right screw', x: 307, y: 28, z: -.035, sign: 'CW', adjustMinutes: 3},
  {key: 'screw3', prettyName: 'Rear right screw', x: 307, y: 314, z: .9333, sign: 'CCW', adjustMinutes: 80},
  {key: 'screw4', prettyName: 'Rear left screw', x: 45, y: 314, z: 0, sign: 'CW', adjustMinutes: 0},
];
function h(tag, props = {}, children = []) {
  if (Array.isArray(props)) {children = props; props = {};}
  return {tag, props, children: children.flat(Infinity)};
}
function complete(status = {}) {
  return {phase: 'complete', running: false, screws, probed_temperature: 70, can_save_mesh: true,
          completed_points: 81, total_points: 81, elapsed: 205, eta: 0, error_message: '', ...status};
}
const context = {
  _self: {_c: h}, _v: String, _s: String, _u: slots => slots, _l: (items, fn) => items.map(fn), _e: () => h('empty'),
  $t: key => ({'app.bedmesh.label.base': 'Base', 'app.general.btn.close': 'Close'}[key] || key),
  screwsTiltAdjust: complete(), open: true, retry: () => {},
  printerBusy: false, hasWaits: false, klippyReady: true, socketConnected: true,
  $waits: {onBedScrewsCalculate: 'onBedScrewsCalculate'}, hasWait: () => context.hasWaits,
  sendGcode: (command, wait) => commands.push({command, wait}),
};
const commands = [];
function all(node) {
  return typeof node === 'string' ? [node] : [node, ...node.children.flatMap(all)];
}
function text(node) {return all(node).filter(item => typeof item === 'string').join(' ');}
function render() {return sandbox.GX.call(context);}

const tree = render();
const positions = Object.fromEntries(all(tree).filter(node => node.props?.key).map(node => [node.props.key, node]));
for (const [key, column, row] of [['screw1', '1', '2'], ['screw2', '2', '2'], ['screw3', '2', '1'], ['screw4', '1', '1']]) {
  assert.equal(positions[key].props.style.gridColumn, column);
  assert.equal(positions[key].props.style.gridRow, row);
}
assert.match(text(positions.screw1), /Base/);
assert.doesNotMatch(text(positions.screw1), /°/);
assert.match(text(positions.screw2), /18°/);
assert.match(text(positions.screw3), /480°/);
assert.match(text(positions.screw4), /0°/);
assert.match(text(positions.screw2), /\$zRotateClockwise/);
assert.match(text(positions.screw3), /\$zRotateCounterclockwise/);
assert.match(text(positions.screw4), /\$success/);
assert.doesNotMatch(text(tree), /00:03|01:20|FRONT|BACK/);
assert.match(text(tree), /PROBED AT 70°C/);
assert.equal(tree.props.on.save, context.retry);
tree.props.model.callback(false);
assert.equal(context.open, false);
// Position follows coordinates, including renamed/reordered screws; input results stay untouched.
const sourceResults = JSON.stringify(screws);
context.screwsTiltAdjust.screws = [...screws].reverse().map(s => ({...s, name: 'same name'}));
for (const node of all(render()).filter(node => node.props?.key)) {
  assert.deepEqual(node.props.style, positions[node.props.key].props.style);
}
assert.equal(JSON.stringify(screws), sourceResults);
// Unexpected arrangements retain the native list instead of guessing positions.
for (const invalid of [[], screws.slice(0, 3), [screws[0], screws[0], screws[2], screws[3]],
                       screws.map(s => ({...s, x: NaN}))]) {
  context.screwsTiltAdjust.screws = invalid;
  const nodes = all(render());
  assert.equal(nodes.find(node => node.tag === 'tbody').props.style.display, undefined);
  assert.equal(nodes.find(node => node.tag === 'thead').props.style.display, undefined);
}
assert.equal(tree.props.attrs['max-width'], '500');
assert.equal(tree.props.attrs.persistent, true);
assert.equal(all(tree).find(node => node.tag === 'table').props.style, undefined);

// Save controls use the existing send API, never submit the Retry form, and wrap locally.
context.screwsTiltAdjust.screws = screws;
function actionButtons(tree) {
  const slot = tree.props.scopedSlots.find(item => item.key === 'actions');
  const root = h('actions', {}, slot.fn());
  return all(root).filter(node => node.tag === 'button');
}
function saveButtons(tree) {
  return actionButtons(tree).filter(node => /^Save /.test(text(node)));
}
const buttons = saveButtons(render());
assert.equal(buttons.length, 2);
assert.ok(buttons.every(button => button.props.attrs.type === 'button'));
assert.deepEqual(buttons.map(text), ['Save bed mesh', 'Save as default bed mesh']);
assert.ok(buttons.every(button => button.props.attrs.disabled === false));
buttons[0].props.on.click();
buttons[1].props.on.click();
assert.deepEqual(commands, [
  {command: 'SCREWS_TILT_SAVE_MESH', wait: 'onBedScrewsCalculate'},
  {command: 'SCREWS_TILT_SAVE_MESH DEFAULT=1', wait: 'onBedScrewsCalculate'},
]);
const actions = render().props.scopedSlots.find(item => item.key === 'actions').fn();
assert.equal(actions[0].props.style.flexWrap, 'wrap');
assert.equal(all(actions[0]).filter(node => node.tag === 'button' && node.props.attrs.type === 'submit').length, 1);

// Read the latest availability again on click, including clicks from an older enabled render.
const saved = {printerBusy: false, hasWaits: false, klippyReady: true, socketConnected: true};
for (const [field, unavailable] of [
  ['printerBusy', true], ['hasWaits', true], ['klippyReady', false], ['socketConnected', false],
]) {
  Object.assign(context, saved, {[field]: unavailable});
  assert.ok(saveButtons(render()).every(button => button.props.attrs.disabled));
  const count = commands.length;
  for (const button of buttons) button.props.on.click();
  assert.equal(commands.length, count);
}
Object.assign(context, saved);
for (const status of [
  {can_save_mesh: false}, {probed_temperature: null}, {probed_temperature: NaN},
  {probed_temperature: Infinity}, {probed_temperature: -1}, {screws: []}, {screws: screws.slice(0, 3)},
  {phase: 'error', error_message: 'Probe failed'}, {phase: 'probing', running: true}, {error: true},
]) {
  context.screwsTiltAdjust = complete(status);
  assert.ok(saveButtons(render()).every(button => button.props.attrs.disabled));
  const count = commands.length;
  for (const button of buttons) button.props.on.click();
  assert.equal(commands.length, count);
  if (Object.hasOwn(status, 'probed_temperature')) assert.doesNotMatch(text(render()), /PROBED AT/);
}
context.screwsTiltAdjust = complete({probed_temperature: 70.26});
assert.match(text(render()), /PROBED AT 70.3°C/);
assert.ok(saveButtons(render()).every(button => !button.props.attrs.disabled));
assert.ok(!source.includes('heater_bed') && !source.includes('$store'));

// Progress uses helper status, not the shared wait token used by saves.
context.hasWaits = true;
assert.ok(all(render()).some(node => node.tag === 'table'));
assert.ok(!all(render()).some(node => node.tag === 'progress'));
context.hasWaits = false;
for (const [phase, caption] of [['homing', 'Homing'], ['probing', 'Probing bed mesh'],
                               ['calculating', 'Calculating screw adjustments']]) {
  context.screwsTiltAdjust = complete({phase, running: true, completed_points: 39, elapsed: 104.9, eta: 106.5});
  const progress = render();
  assert.match(text(progress), new RegExp(caption));
  assert.match(text(progress), /39 \/ 81 points/);
  assert.doesNotMatch(text(progress), /Elapsed:/);
  assert.match(text(progress), /Estimated remaining: 1m 46s/);
  assert.ok(!all(progress).some(node => node.tag === 'table'));
  assert.equal(saveButtons(progress).length, 0);
  const bar = all(progress).find(node => node.tag === 'progress');
  assert.equal(bar.props.attrs.indeterminate, phase === 'homing');
  assert.equal(bar.props.attrs.value, 39 / 81 * 100);
  assert.ok(actionButtons(progress).find(button => button.props.attrs.type === 'submit').props.attrs.disabled);
  const close = actionButtons(progress).find(button => text(button) === 'Close');
  context.open = true;
  close.props.on.click();
  assert.equal(context.open, false);
}
context.screwsTiltAdjust = complete({phase: 'probing', running: true, eta: null, completed_points: 81});
assert.match(text(render()), /Estimated remaining: Updating estimate…/);
assert.equal(all(render()).find(node => node.tag === 'progress').props.attrs.value, 99);
context.screwsTiltAdjust = complete({phase: 'probing', running: true, eta: null, completed_points: 1});
assert.match(text(render()), /Estimated remaining: Not available yet/);
context.screwsTiltAdjust = complete({phase: 'probing', running: true, total_points: NaN,
                                   completed_points: Infinity, elapsed: NaN, eta: -1});
assert.match(text(render()), /0 \/ \? points/);
assert.doesNotMatch(text(render()), /NaN|Infinity|-1m/);
assert.equal(all(render()).find(node => node.tag === 'progress').props.attrs.indeterminate, true);
context.screwsTiltAdjust = complete({phase: 'error', can_save_mesh: false, error_message: 'Nozzle contact failed'});
assert.match(text(render()), /Screw adjustment failed/);
assert.match(text(render()), /Nozzle contact failed/);
assert.equal(saveButtons(render()).length, 0);
assert.ok(!all(render()).some(node => node.tag === 'table'));
context.screwsTiltAdjust = complete();
assert.ok(all(render()).some(node => node.tag === 'table'));

// Popup-only hooks keep progress/results open, honor explicit dismissal and retain live status.
const dialogEdits = JSON.parse(fs.readFileSync(path.join(__dirname, '../app/assets/screws-dialog-edits.json'), 'utf8'));
const hookSandbox = {f: (target, key, value) => {target[key] = value;}};
vm.createContext(hookSandbox);
vm.runInContext('this.Tool=class{constructor(){' + dialogEdits[0][1] + '}' +
                dialogEdits[1][1].replace(/get forceMove\(\)$/, '') + dialogEdits[2][1] + '}', hookSandbox);
const tool = new hookSandbox.Tool();
tool.$store = {state: {printer: {printer: {screws_tilt_adjust: {running: true, phase: 'probing'}}},
                      config: {uiSettings: {general: {showScrewsTiltAdjustDialogAutomatically: true}}}}};
tool.klippyReady = true;
tool.printerPrinting = false;
assert.equal(tool.screwsTiltAdjustRunning, true);
tool.onScrewsTiltAdjustRunning(tool.screwsTiltAdjustRunning);
assert.equal(tool.screwsTiltAdjustDialogOpen, true);
tool.onHasScrewsTiltAdjustResults(false);
tool.onScrewsTiltAdjustRunning(false);
tool.onScrewsTiltAdjustFailed(false);
assert.equal(tool.screwsTiltAdjustDialogOpen, true);
const closeModel = vm.runInContext('({' + dialogEdits[4][1] + '})', hookSandbox).callback;
hookSandbox.e = tool;
closeModel(false);
tool.onHasScrewsTiltAdjustResults(true);
tool.$store.state.printer.printer.screws_tilt_adjust = {running: false, phase: 'error'};
tool.onScrewsTiltAdjustFailed(tool.screwsTiltAdjustFailed);
assert.equal(tool.screwsTiltAdjustDialogOpen, false);
tool.$store.state.printer.printer.screws_tilt_adjust = {running: true, phase: 'homing'};
tool.onScrewsTiltAdjustRunning(tool.screwsTiltAdjustRunning);
assert.equal(tool.screwsTiltAdjustDialogOpen, true);
assert.equal(tool.screwsTiltAdjustDialogDismissed, false);
tool.klippyReady = false;
assert.equal(tool.screwsTiltAdjustRunning, false);
tool.klippyReady = true;
assert.equal(tool.screwsTiltAdjustRunning, true);
tool.printerPrinting = true;
assert.equal(tool.screwsTiltAdjustRunning, false);
tool.printerPrinting = false;
tool.$store.state.config.uiSettings.general.showScrewsTiltAdjustDialogAutomatically = false;
assert.equal(tool.screwsTiltAdjustRunning, false);

const watchers = [];
hookSandbox.Pe = (field, options) => ({field, options});
hookSandbox.Cd = (decorators, prototype, handler) => watchers.push({...decorators[0], handler});
hookSandbox.ka = hookSandbox.Tool;
vm.runInContext(dialogEdits[3][1], hookSandbox);
assert.equal(watchers.find(watch => watch.field === 'screwsTiltAdjustRunning').options.immediate, true);
assert.equal(watchers.find(watch => watch.field === 'screwsTiltAdjustFailed').options.immediate, true);

vm.runInContext('this.Popup=class{constructor(){this.open=true}' + dialogEdits[5][1] + dialogEdits[6][1] + '}', hookSandbox);
const popup = new hookSandbox.Popup();
Object.assign(popup, saved, {screwsTiltAdjust: complete(), $waits: context.$waits,
                           sendGcode: context.sendGcode, $store: {state: {printer: {printer: {}}},
                           commit: (name, change) => {
                             commands.push({commit: name});
                             if (name === 'printer/setSocketNotify') {
                               const state = popup.$store.state.printer.printer;
                               Object.assign(state[change.key] ||= {}, change.payload);
                             }
                           }}});
let count = commands.length;
popup.retry();
assert.equal(commands.length, count + 1);
assert.equal(commands.at(-1).command, 'SCREWS_TILT_CALCULATE');
assert.equal(popup.open, true);
popup.screwsTiltAdjust = complete({phase: 'probing', running: true});
popup.$store.state.printer.printer.screws_tilt_adjust = popup.screwsTiltAdjust;
count = commands.length;
popup.retry();
popup.destroyed();
assert.equal(commands.length, count);
for (const phase of ['complete', 'error']) {
  const status = complete({phase, results: {screw1: {z: 1}}, error: phase === 'error',
                           error_message: 'Previous result', firmware_field: 'keep'});
  popup.screwsTiltAdjust = status;
  popup.$store.state.printer.printer.screws_tilt_adjust = status;
  popup.destroyed();
  assert.equal(commands.at(-1).commit, 'printer/setSocketNotify');
  assert.equal(popup.$store.state.printer.printer.screws_tilt_adjust, status);
  assert.equal(status.total_points, 81);
  assert.equal(status.firmware_field, 'keep');
  assert.equal(status.phase, 'idle');
  assert.equal(Object.keys(status.results).length, 0);
  assert.equal(status.error, false);
  assert.equal(status.error_message, '');
  assert.equal(status.can_save_mesh, false);
  // Native status notifications resend changed fields, not the unchanged total.
  Object.assign(status, {phase: 'probing', running: true, completed_points: 39, elapsed: 100, eta: 110});
  context.screwsTiltAdjust = {...status, screws: []};
  assert.match(text(render()), /39 \/ 81 points/);
}
Object.defineProperty(popup, 'screwsTiltAdjust', {get() {throw new Error('Config getter unavailable during teardown');}});
popup.$store.state.printer.printer.screws_tilt_adjust = undefined;
assert.doesNotThrow(() => popup.destroyed());

const subscribe = vm.runInContext('(function(s){' + dialogEdits[7][1].replace(/,$/, ';') + 'return s})', hookSandbox);
for (const running of [false, undefined]) {
  const status = {running, results: {screw1: {z: 1}}, total_points: 81, completed_points: 0,
                  phase: 'error', error: true, error_message: 'Previous error', can_save_mesh: true,
                  elapsed: 0, eta: null, firmware_field: 'keep'};
  const subscription = {screws_tilt_adjust: status};
  assert.equal(subscribe(subscription).screws_tilt_adjust, status);
  assert.equal(status.total_points, 81);
  assert.equal(status.firmware_field, 'keep');
  assert.equal(status.phase, 'idle');
  assert.equal(Object.keys(status.results).length, 0);
  assert.equal(status.error, false);
  assert.equal(status.error_message, '');
  assert.equal(status.can_save_mesh, false);
  Object.assign(status, {phase: 'probing', running: true, completed_points: 39, elapsed: 100, eta: 110});
  context.screwsTiltAdjust = {...status, screws: []};
  assert.match(text(render()), /39 \/ 81 points/);
}
const activeSubscription = {screws_tilt_adjust: {running: true, phase: 'probing', completed_points: 39,
                                               total_points: 81, results: {screw1: {z: 1}}}};
const activeStatus = activeSubscription.screws_tilt_adjust;
assert.equal(subscribe(activeSubscription).screws_tilt_adjust.completed_points, 39);
assert.equal(activeSubscription.screws_tilt_adjust, activeStatus);
assert.equal(Object.keys(activeStatus.results).length, 1);
assert.equal(activeStatus.phase, 'probing');

// The cached native bundle is optional outside local development; every patch must match once.
const nativePath = path.join(__dirname, '../build/screw-helper-reference/index-DAJGCJUz.js');
if (fs.existsSync(nativePath)) {
  let native = fs.readFileSync(nativePath, 'utf8');
  for (const [before, after] of dialogEdits) {
    assert.equal(native.split(before).length - 1, 1);
    native = native.replace(before, after);
  }
  assert.ok(native.includes('"v-dialog",e.$attrs,!1'));
}
console.log('Popup passed: persistent progress/error/results, grid/degrees, native actions and preserved totals across idle subscription/teardown.');
