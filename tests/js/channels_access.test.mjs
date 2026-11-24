import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = fs.readFileSync(new URL('../../modules/web_viewer/templates/channels.html', import.meta.url), 'utf8');
const start = html.indexOf('    async function loadChannelData()');
const end = html.indexOf('    async function load()', start);
assert.ok(start >= 0 && end > start);

async function load(responses) {
    const calls = [];
    const context = vm.createContext({ fetch: async (path, options) => {
        calls.push({path, options});
        const response = responses.shift();
        if (response instanceof Error) throw response;
        return response;
    }});
    vm.runInContext(html.slice(start, end), context);
    return { data: await context.loadChannelData(), calls };
}

const response = (channels, is_admin = false) => ({ok: true, json: async () => ({channels, is_admin})});

test('existing credentials load the complete view without a second request', async () => {
    const result = await load([response(['published', 'private'], true)]);
    assert.deepEqual(result.data.channels, ['published', 'private']);
    assert.equal(result.calls.length, 1);
    assert.equal(result.calls[0].path, '/channels/settings/view');
    assert.equal(result.calls[0].options.credentials, 'same-origin');
    assert.equal(result.calls[0].options.cache, 'no-store');
    assert.equal(result.calls[0].options.redirect, 'error');
});

test('anonymous probe rejection loads only the public feed', async () => {
    const result = await load([{ok: false, status: 401}, response(['published'])]);
    assert.deepEqual(result.data.channels, ['published']);
    assert.equal(result.data.is_admin, false);
    assert.deepEqual(result.calls.map(call => call.path), ['/channels/settings/view', '/api/channels/published']);
});

for (const failure of [new Error('network failure'), {ok: true, json: async () => { throw new Error('HTML login page'); }},
    {ok: true, json: async () => ({error: 'Unavailable'})}]) {
    test('unavailable or malformed private response falls back to publication', async () => {
        const result = await load([failure, response(['published'])]);
        assert.deepEqual(result.data.channels, ['published']);
    });
}

test('empty published feed stays empty', async () => {
    const result = await load([{ok: false, status: 401}, response([])]);
    assert.deepEqual(result.data.channels, []);
});

test('failure of both feeds reports unavailable rather than using stale private data', async () => {
    await assert.rejects(load([{ok: false, status: 401}, {ok: false, status: 503}]), /Channels unavailable/);
});
