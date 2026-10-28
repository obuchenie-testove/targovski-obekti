<?php
/*
 * По желание: позволява на „Търговски обекти“ да чете началните страници на сайтовете на обектите.
 * 1. Смени КЛЮЧА отдолу с дълга случайна дума.
 * 2. Качи файла на хостинга си (до index.html или където и да е).
 * 3. В системата: Настройки → адрес на proxy.php + същия ключ.
 */
const KEY = 'СМЕНИ-МЕ-с-дълъг-случаен-ключ';
const MAX_BYTES = 600000;

header('Access-Control-Allow-Origin: *');
header('Content-Type: text/html; charset=utf-8');

if (KEY === 'СМЕНИ-МЕ-с-дълъг-случаен-ключ' || !hash_equals(KEY, (string)($_GET['key'] ?? ''))) {
    http_response_code(403); exit('forbidden');
}
// Взима една страница, като проверява всеки адрес (и пренасочванията) срещу вътрешни мрежи
function fetch_once(string $url, ?string &$location): array {
    $p = parse_url($url);
    if (!$p || !in_array(strtolower($p['scheme'] ?? ''), ['http', 'https'], true) || empty($p['host'])) return [400, ''];
    $ips = gethostbynamel($p['host']) ?: [];
    if (!$ips) return [502, ''];
    foreach ($ips as $ip) {
        if (!filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE)) return [400, ''];
    }
    $port = (int)($p['port'] ?? (strtolower($p['scheme']) === 'https' ? 443 : 80));
    if (!in_array($port, [80, 443], true)) return [400, ''];
    $buf = '';
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RESOLVE => [$p['host'] . ':' . $port . ':' . $ips[0]],
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_TIMEOUT => 10,
        CURLOPT_CONNECTTIMEOUT => 5,
        CURLOPT_USERAGENT => 'TargovskiObekti/1.0',
        CURLOPT_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
        CURLOPT_WRITEFUNCTION => function ($ch, $chunk) use (&$buf) {
            $buf .= $chunk;
            return strlen($buf) > MAX_BYTES ? 0 : strlen($chunk);
        },
    ]);
    curl_exec($ch);
    $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $location = curl_getinfo($ch, CURLINFO_REDIRECT_URL) ?: null;
    curl_close($ch);
    return [$code, $buf];
}

$url = (string)($_GET['url'] ?? '');
for ($i = 0; $i < 4; $i++) {
    $loc = null;
    [$code, $body] = fetch_once($url, $loc);
    if ($code >= 300 && $code < 400 && $loc) { $url = $loc; continue; }
    break;
}
if ($code < 200 || $code >= 300 || $body === '') { http_response_code($code === 400 ? 400 : 502); exit('unreachable'); }
echo substr($body, 0, MAX_BYTES);
