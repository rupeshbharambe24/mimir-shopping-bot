import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';

/// Thin client for the FastAPI BFF. A per-tab session id is generated once and
/// sent as `X-Session-Id` on every REST call and as `?sid=` on the WebSocket,
/// so the browser doesn't need cross-origin cookies.
class Api {
  Api({String? baseUrl})
      : baseUrl = baseUrl ?? const String.fromEnvironment('BFF_URL', defaultValue: 'http://127.0.0.1:8011'),
        sid = _randomSid();

  final String baseUrl;
  final String sid;

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        'X-Session-Id': sid,
      };

  static String _randomSid() =>
      'sid_${DateTime.now().microsecondsSinceEpoch}_${(DateTime.now().hashCode & 0xffff)}';

  Future<Map<String, dynamic>> providers() async {
    final r = await http.get(Uri.parse('$baseUrl/providers'), headers: _headers);
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> selectProvider(String id) async {
    final r = await http.post(Uri.parse('$baseUrl/providers/$id/select'), headers: _headers);
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> connectProvider(String id) async {
    final r = await http.post(Uri.parse('$baseUrl/providers/$id/connect'), headers: _headers);
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> compare(String query) async {
    final r = await http.post(
      Uri.parse('$baseUrl/compare'),
      headers: _headers,
      body: jsonEncode({'query': query}),
    );
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> matrix({bool live = false}) async {
    final r = await http.get(Uri.parse('$baseUrl/matrix?live=$live'), headers: _headers);
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> state() async {
    final r = await http.get(Uri.parse('$baseUrl/state'), headers: _headers);
    return jsonDecode(r.body) as Map<String, dynamic>;
  }

  WebSocketChannel openChat() {
    final wsBase = baseUrl.replaceFirst('http', 'ws');
    return WebSocketChannel.connect(Uri.parse('$wsBase/chat?sid=$sid'));
  }
}
