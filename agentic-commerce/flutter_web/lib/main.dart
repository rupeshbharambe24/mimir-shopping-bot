import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import 'api.dart';

void main() => runApp(const AgenticCommerceApp());

class AgenticCommerceApp extends StatelessWidget {
  const AgenticCommerceApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Agentic Commerce',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorSchemeSeed: const Color(0xFF2942C9),
        brightness: Brightness.light,
      ),
      home: const HomePage(),
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final Api api = Api();
  List<Map<String, dynamic>> _providers = [];
  String? _selected;
  final Set<String> _connected = {};
  String _status = 'Loading providers…';

  @override
  void initState() {
    super.initState();
    _loadProviders();
  }

  Future<void> _loadProviders() async {
    try {
      final data = await api.providers();
      setState(() {
        _providers = (data['providers'] as List).cast<Map<String, dynamic>>();
        _selected = data['selected'] as String? ??
            (_providers.isNotEmpty ? _providers.first['id'] as String? : null);
        for (final p in _providers) {
          if (p['connected'] == true) _connected.add(p['id'] as String);
        }
        _status = 'Ready';
      });
    } catch (e) {
      setState(() => _status = 'Cannot reach backend at ${api.baseUrl} — is it running? ($e)');
    }
  }

  Future<void> _select(String id) async {
    await api.selectProvider(id);
    setState(() => _selected = id);
  }

  Future<void> _connect(String id) async {
    setState(() => _status = 'Connecting $id…');
    final res = await api.connectProvider(id);
    setState(() {
      if (res['ok'] == true) {
        _connected.add(id);
        _status = 'Connected $id';
      } else {
        _status = 'Connect failed: ${res['error']}';
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return DefaultTabController(
      length: 3,
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Agentic Commerce'),
          bottom: const TabBar(tabs: [
            Tab(icon: Icon(Icons.chat_bubble_outline), text: 'Chat'),
            Tab(icon: Icon(Icons.compare_arrows), text: 'Compare'),
            Tab(icon: Icon(Icons.grid_view), text: 'Capability Matrix'),
          ]),
        ),
        body: Column(
          children: [
            _providerBar(),
            Expanded(
              child: TabBarView(children: [
                ChatTab(api: api, selected: _selected, connected: _connected),
                CompareTab(api: api),
                MatrixTab(api: api),
              ]),
            ),
          ],
        ),
      ),
    );
  }

  Widget _providerBar() {
    return Material(
      color: Theme.of(context).colorScheme.surfaceContainerHighest,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
        child: Row(
          children: [
            const Text('Provider:', style: TextStyle(fontWeight: FontWeight.w600)),
            const SizedBox(width: 12),
            DropdownButton<String>(
              value: _selected,
              items: [
                for (final p in _providers)
                  DropdownMenuItem(value: p['id'] as String, child: Text(p['display_name'] as String)),
              ],
              onChanged: (v) => v == null ? null : _select(v),
            ),
            const SizedBox(width: 12),
            if (_selected != null)
              _connected.contains(_selected)
                  ? const Chip(
                      avatar: Icon(Icons.check_circle, color: Colors.green, size: 18),
                      label: Text('connected'))
                  : FilledButton.tonal(
                      onPressed: () => _connect(_selected!),
                      child: const Text('Connect'),
                    ),
            const Spacer(),
            Text(_status, style: Theme.of(context).textTheme.bodySmall),
          ],
        ),
      ),
    );
  }
}

// --------------------------------------------------------------------------
// Chat tab
// --------------------------------------------------------------------------

class ChatTab extends StatefulWidget {
  const ChatTab({super.key, required this.api, required this.selected, required this.connected});
  final Api api;
  final String? selected;
  final Set<String> connected;

  @override
  State<ChatTab> createState() => _ChatTabState();
}

class _ChatEvent {
  _ChatEvent(this.kind, this.text, {this.error = false});
  final String kind; // you | agent | tool | error | system
  final String text;
  final bool error;
}

class _ChatTabState extends State<ChatTab> {
  WebSocketChannel? _ch;
  final _events = <_ChatEvent>[];
  final _input = TextEditingController();
  bool _connected = false;

  void _log(_ChatEvent e) => setState(() => _events.add(e));

  void _openSocket() {
    if (widget.selected == null || !widget.connected.contains(widget.selected)) {
      _log(_ChatEvent('system', 'Select and Connect a provider first (top bar).', error: true));
      return;
    }
    final ch = widget.api.openChat();
    _ch = ch;
    _log(_ChatEvent('system', 'Connecting to agent…'));
    ch.stream.listen((raw) {
      final ev = jsonDecode(raw as String) as Map<String, dynamic>;
      switch (ev['type']) {
        case 'ready':
          setState(() => _connected = true);
          _log(_ChatEvent('system', 'Agent ready on ${ev['provider']} (${ev['tools']} tools).'));
          break;
        case 'tool_call':
          _log(_ChatEvent('tool', '🔧 ${ev['name']}(${jsonEncode(ev['args'])})'));
          break;
        case 'tool_result':
          _log(_ChatEvent('tool', '↩︎ ${_truncate(ev['text'] as String? ?? '')}',
              error: ev['isError'] == true));
          break;
        case 'text':
          _log(_ChatEvent('agent', ev['content'] as String? ?? ''));
          break;
        case 'armed':
          _log(_ChatEvent('system', '🔓 ${ev['message']}'));
          break;
        case 'error':
          _log(_ChatEvent('error', ev['message'] as String? ?? 'error', error: true));
          break;
      }
    }, onError: (e) => _log(_ChatEvent('error', '$e', error: true)),
       onDone: () => setState(() => _connected = false));
  }

  String _truncate(String s) => s.length > 240 ? '${s.substring(0, 240)}…' : s;

  void _send() {
    final text = _input.text.trim();
    if (text.isEmpty || _ch == null) return;
    _ch!.sink.add(jsonEncode({'type': 'message', 'text': text}));
    _log(_ChatEvent('you', text));
    _input.clear();
  }

  void _confirmCheckout() {
    _ch?.sink.add(jsonEncode({'type': 'confirm_checkout'}));
  }

  @override
  void dispose() {
    _ch?.sink.close();
    _input.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        if (!_connected)
          Padding(
            padding: const EdgeInsets.all(12),
            child: FilledButton.icon(
              onPressed: _openSocket,
              icon: const Icon(Icons.play_arrow),
              label: const Text('Start agent session'),
            ),
          ),
        Expanded(
          child: ListView.builder(
            padding: const EdgeInsets.all(12),
            itemCount: _events.length,
            itemBuilder: (_, i) => _bubble(_events[i]),
          ),
        ),
        const Divider(height: 1),
        Padding(
          padding: const EdgeInsets.all(10),
          child: Row(
            children: [
              Tooltip(
                message: 'Arms order placement for your next message (DRY_RUN fakes it).',
                child: OutlinedButton.icon(
                  onPressed: _connected ? _confirmCheckout : null,
                  icon: const Icon(Icons.lock_open),
                  label: const Text('Confirm & place order'),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: TextField(
                  controller: _input,
                  onSubmitted: (_) => _send(),
                  decoration: const InputDecoration(
                    hintText: 'e.g. add 2 packs of toned milk and show my cart',
                    border: OutlineInputBorder(),
                    isDense: true,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              IconButton.filled(onPressed: _connected ? _send : null, icon: const Icon(Icons.send)),
            ],
          ),
        ),
      ],
    );
  }

  Widget _bubble(_ChatEvent e) {
    final theme = Theme.of(context);
    Color? bg;
    Alignment align = Alignment.centerLeft;
    switch (e.kind) {
      case 'you':
        bg = theme.colorScheme.primaryContainer;
        align = Alignment.centerRight;
        break;
      case 'agent':
        bg = theme.colorScheme.surfaceContainerHighest;
        break;
      case 'tool':
        bg = theme.colorScheme.surfaceContainerLow;
        break;
      case 'error':
        bg = theme.colorScheme.errorContainer;
        break;
      default:
        bg = null;
    }
    return Align(
      alignment: align,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 9),
        constraints: const BoxConstraints(maxWidth: 620),
        decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(12)),
        child: Text(
          e.text,
          style: e.kind == 'tool'
              ? const TextStyle(fontFamily: 'monospace', fontSize: 12.5)
              : null,
        ),
      ),
    );
  }
}

// --------------------------------------------------------------------------
// Compare tab
// --------------------------------------------------------------------------

class CompareTab extends StatefulWidget {
  const CompareTab({super.key, required this.api});
  final Api api;
  @override
  State<CompareTab> createState() => _CompareTabState();
}

class _CompareTabState extends State<CompareTab> {
  final _q = TextEditingController(text: 'milk');
  Map<String, dynamic>? _result;
  bool _loading = false;

  Future<void> _run() async {
    setState(() => _loading = true);
    final res = await widget.api.compare(_q.text.trim());
    setState(() {
      _result = res;
      _loading = false;
    });
  }

  Color _statusColor(String s) => switch (s) {
        'ok' => Colors.green,
        'needs_connection' => Colors.orange,
        'timed_out' => Colors.blueGrey,
        _ => Colors.red,
      };

  @override
  Widget build(BuildContext context) {
    final rows = (_result?['rows'] as List?)?.cast<Map<String, dynamic>>() ?? [];
    return Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(children: [
            Expanded(
              child: TextField(
                controller: _q,
                onSubmitted: (_) => _run(),
                decoration: const InputDecoration(
                  labelText: 'Search all providers', border: OutlineInputBorder(), isDense: true),
              ),
            ),
            const SizedBox(width: 10),
            FilledButton(onPressed: _loading ? null : _run, child: const Text('Compare')),
          ]),
          const SizedBox(height: 16),
          if (_loading) const LinearProgressIndicator(),
          Expanded(
            child: ListView(
              children: [
                for (final r in rows) _providerCard(r),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _providerCard(Map<String, dynamic> r) {
    final status = r['status'] as String;
    final items = (r['items'] as List?)?.cast<Map<String, dynamic>>() ?? [];
    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              Text(r['display_name'] as String,
                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
              const SizedBox(width: 10),
              Chip(
                backgroundColor: _statusColor(status).withValues(alpha: 0.15),
                label: Text(status, style: TextStyle(color: _statusColor(status))),
                visualDensity: VisualDensity.compact,
              ),
            ]),
            if (r['error'] != null)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(r['error'] as String, style: const TextStyle(color: Colors.red, fontSize: 12)),
              ),
            for (final it in items)
              ListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                title: Text(it['name'] as String? ?? ''),
                subtitle: Text('${it['unit'] ?? ''}${it['available'] == false ? '  • out of stock' : ''}'),
                trailing: Text(it['price_display'] as String? ?? '',
                    style: const TextStyle(fontWeight: FontWeight.w600)),
              ),
          ],
        ),
      ),
    );
  }
}

// --------------------------------------------------------------------------
// Capability matrix tab
// --------------------------------------------------------------------------

class MatrixTab extends StatefulWidget {
  const MatrixTab({super.key, required this.api});
  final Api api;
  @override
  State<MatrixTab> createState() => _MatrixTabState();
}

class _MatrixTabState extends State<MatrixTab> {
  List<Map<String, dynamic>> _rows = [];
  bool _live = false;
  bool _loading = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => _loading = true);
    final res = await widget.api.matrix(live: _live);
    setState(() {
      _rows = (res['rows'] as List).cast<Map<String, dynamic>>();
      _loading = false;
    });
  }

  Icon _boolIcon(bool v) =>
      v ? const Icon(Icons.check, color: Colors.green, size: 18) : const Icon(Icons.close, color: Colors.red, size: 18);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(children: [
            const Text('Capability Matrix (FR3)', style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
            const SizedBox(width: 16),
            Switch(value: _live, onChanged: (v) { setState(() => _live = v); _load(); }),
            const Text('live tools/list check'),
          ]),
          const SizedBox(height: 12),
          if (_loading) const LinearProgressIndicator(),
          Expanded(
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: SingleChildScrollView(
                child: DataTable(
                  columns: const [
                    DataColumn(label: Text('Provider')),
                    DataColumn(label: Text('Anon search')),
                    DataColumn(label: Text('Cart')),
                    DataColumn(label: Text('Checkout')),
                    DataColumn(label: Text('Pre-confirm')),
                    DataColumn(label: Text('Cap')),
                    DataColumn(label: Text('Tools')),
                    DataColumn(label: Text('Drift OK')),
                  ],
                  rows: [
                    for (final r in _rows)
                      DataRow(cells: [
                        DataCell(Text(r['display_name'] as String)),
                        DataCell(_boolIcon((r['flags'] as Map)['searchable_without_auth'] == true)),
                        DataCell(_boolIcon((r['flags'] as Map)['supports_cart'] == true)),
                        DataCell(_boolIcon((r['flags'] as Map)['supports_checkout'] == true)),
                        DataCell(_boolIcon((r['flags'] as Map)['requires_pre_confirmation'] == true)),
                        DataCell(Text(_cap((r['flags'] as Map)['max_order_value']))),
                        DataCell(Text(r['tool_count']?.toString() ?? '—')),
                        DataCell(r['drift_ok'] == null
                            ? const Text('—')
                            : _boolIcon(r['drift_ok'] == true)),
                      ]),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  String _cap(dynamic paise) => paise == null ? '—' : '₹${(paise as int) ~/ 100}';
}
