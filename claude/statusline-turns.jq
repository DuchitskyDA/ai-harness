# Turn pass of the statusline layout (statusline-command.sh includes it via -L):
# one stream over the transcript gives the turn count (n) and tok/s of the last
# turn with output: per API response from api-request (or the user
# record before it) to its last assistant record, tool time excluded.
def tsec: (.[0:19] + "Z" | fromdateiso8601) + ((.[19:23] | select(startswith(".")) | "0" + . | tonumber) // 0);
def tsof: try (.timestamp | tsec) catch null;
def fin: if .m != null then .c.t += .m.t | .c.s += ([.m.e - .m.b, 0] | max) | .m = null else . end;
def turns: (reduce inputs as $l ({n: 0, c: {t: 0, s: 0}, v: {t: 0, s: 0}, b: null, m: null};
    (try ($l | fromjson) catch null) as $e
    | if ($e | type) != "object" then .
      elif $e.type == "user" and ($e | has("toolUseResult") | not) and ($e.isMeta != true) then
        fin | .n += 1 | (if .c.t > 0 then .v = .c else . end) | .c = {t: 0, s: 0} | .b = ($e | tsof)
      elif $e.type == "user" then .b = ($e | tsof)
      elif $e.type == "api-request" and (($e.querySource // "repl_main_thread") == "repl_main_thread") then fin | .b = ($e | tsof)
      elif $e.type == "assistant" and (($e.message.usage.output_tokens? | type) == "number") then
        ($e | tsof) as $t | ($e.message.id // "") as $id | $e.message.usage.output_tokens as $o
        | if $t == null then .
          elif .m != null and .m.id == $id then .m.t = $o | .m.e = $t
          else fin | (if .b != null then .m = {id: $id, t: $o, b: .b, e: $t} else . end) end
      else . end) | fin);
