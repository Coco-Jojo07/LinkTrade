//+------------------------------------------------------------------+
//| ViewLinkEA.mq5                                                   |
//| ViewLink Local - polls the local bridge and executes signals     |
//| that the trader confirmed in the Chrome extension.               |
//|                                                                  |
//| Setup: Tools > Options > Expert Advisors > "Allow WebRequest for |
//| listed URL" and add http://127.0.0.1:8787                        |
//+------------------------------------------------------------------+
#property copyright   "ViewLink Local"
#property version     "1.00"
#property description "Executes trades confirmed in the ViewLink Chrome extension."
#property description "Polls a local bridge (127.0.0.1). Every signal is validated again here."

#include <Trade/Trade.mqh>

//--- inputs
input string InpBridgeUrl     = "http://127.0.0.1:8787"; // Bridge URL (add it to the WebRequest list)
input string InpApiKey        = "";                      // API key printed by bridge/server.py
input double InpMaxLots       = 1.0;                     // Hard cap on lots per order
input uint   InpDeviation     = 20;                      // Max deviation for market orders (points)
input ulong  InpMagic         = 870787;                  // Magic number
input int    InpPollMs        = 1000;                    // Polling interval (ms, >= 250)
input bool   InpAllowMarket   = true;                    // Allow market orders
input bool   InpAllowPending  = true;                    // Allow limit/stop orders

//--- constants
#define HTTP_TIMEOUT_MS 3000
#define MAX_JSON_FIELDS 32

//--- a flat signal as delivered by GET /next
struct ViewLinkSignal
  {
   string            id;
   string            symbol;
   string            side;
   string            order;
   double            lots;
   double            entry;
   double            sl;
   double            tp;
   string            comment;
  };

//--- an execution report sent to POST /result
struct ViewLinkReport
  {
   bool              ok;
   uint              retcode;
   ulong             ticket;
   string            message;
  };

CTrade g_trade;
string g_baseUrl   = "";
string g_lastError = "";   // used to avoid flooding the log with repeated errors

//+------------------------------------------------------------------+
//| Logging helpers                                                  |
//+------------------------------------------------------------------+
void LogInfo(const string text)
  {
   Print("ViewLink: ", text);
  }

// Logs an error only when it differs from the previous one (polling noise).
void LogErrorOnce(const string text)
  {
   if(text == g_lastError)
      return;
   g_lastError = text;
   Print("ViewLink ERROR: ", text);
  }

void ClearLastError()
  {
   if(g_lastError != "")
      LogInfo("connection to the bridge is OK again");
   g_lastError = "";
  }

//+------------------------------------------------------------------+
//| HTTP                                                             |
//+------------------------------------------------------------------+
// Performs a request. Returns the HTTP status, or -1 on transport error.
int HttpRequest(const string method, const string path, const string body, string &response)
  {
   char   data[];
   char   result[];
   string resultHeaders;
   string headers = "X-API-Key: " + InpApiKey + "\r\n";

   if(body != "")
     {
      headers += "Content-Type: application/json\r\n";
      int len = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
      if(len > 0)
         ArrayResize(data, len - 1);   // drop the terminating zero
     }

   ResetLastError();
   int status = WebRequest(method, g_baseUrl + path, headers, HTTP_TIMEOUT_MS, data, result, resultHeaders);
   if(status == -1)
     {
      int err = GetLastError();
      if(err == 4014)
         LogErrorOnce("WebRequest is not allowed for " + g_baseUrl +
                      ". Add it in Tools > Options > Expert Advisors > Allow WebRequest for listed URL.");
      else
         LogErrorOnce("WebRequest to " + g_baseUrl + path + " failed (error " + IntegerToString(err) +
                      "). Is bridge/server.py running? Also check that the URL is in the WebRequest list.");
      return -1;
     }
   response = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   if(status == 401)
     {
      LogErrorOnce("API key rejected by the bridge (401). Copy the key printed by server.py into InpApiKey.");
      return status;
     }
   return status;
  }

//+------------------------------------------------------------------+
//| Minimal flat JSON parser                                         |
//| Supports one object of string / number / true / false / null     |
//| values. Nested objects or arrays are rejected.                   |
//+------------------------------------------------------------------+
int SkipSpaces(const string s, int pos)
  {
   int n = StringLen(s);
   while(pos < n)
     {
      ushort c = StringGetCharacter(s, pos);
      if(c != ' ' && c != '\t' && c != '\r' && c != '\n')
         break;
      pos++;
     }
   return pos;
  }

int HexDigit(const ushort c)
  {
   if(c >= '0' && c <= '9')
      return (int)(c - '0');
   if(c >= 'a' && c <= 'f')
      return (int)(c - 'a') + 10;
   if(c >= 'A' && c <= 'F')
      return (int)(c - 'A') + 10;
   return -1;
  }

// Parses a JSON string starting at the opening quote. Updates pos.
bool ParseJsonString(const string s, int &pos, string &out)
  {
   int n = StringLen(s);
   out = "";
   if(pos >= n || StringGetCharacter(s, pos) != '"')
      return false;
   pos++;
   while(pos < n)
     {
      ushort c = StringGetCharacter(s, pos);
      if(c == '"')
        {
         pos++;
         return true;
        }
      if(c == '\\')
        {
         if(pos + 1 >= n)
            return false;
         ushort e = StringGetCharacter(s, pos + 1);
         pos += 2;
         if(e == 'n')
            out += "\n";
         else if(e == 't')
            out += "\t";
         else if(e == 'r')
            out += "\r";
         else if(e == 'b' || e == 'f')
            out += " ";
         else if(e == 'u')
           {
            if(pos + 4 > n)
               return false;
            int code = 0;
            for(int i = 0; i < 4; i++)
              {
               int d = HexDigit(StringGetCharacter(s, pos + i));
               if(d < 0)
                  return false;
               code = code * 16 + d;
              }
            pos += 4;
            out += ShortToString((ushort)code);
           }
         else
            out += ShortToString(e);   // \" \\ \/
         continue;
        }
      out += ShortToString(c);
      pos++;
     }
   return false;   // unterminated string
  }

// Parses a flat JSON object into parallel key/value arrays.
bool ParseFlatJson(const string s, string &keys[], string &values[])
  {
   ArrayResize(keys, 0);
   ArrayResize(values, 0);
   int n = StringLen(s);
   int pos = SkipSpaces(s, 0);
   if(pos >= n || StringGetCharacter(s, pos) != '{')
      return false;
   pos = SkipSpaces(s, pos + 1);
   if(pos < n && StringGetCharacter(s, pos) == '}')
      return true;   // empty object

   while(pos < n)
     {
      string key, value;
      if(!ParseJsonString(s, pos, key))
         return false;
      pos = SkipSpaces(s, pos);
      if(pos >= n || StringGetCharacter(s, pos) != ':')
         return false;
      pos = SkipSpaces(s, pos + 1);
      if(pos >= n)
         return false;

      ushort c = StringGetCharacter(s, pos);
      if(c == '{' || c == '[')
         return false;   // nested values are not supported
      if(c == '"')
        {
         if(!ParseJsonString(s, pos, value))
            return false;
        }
      else
        {
         int start = pos;
         while(pos < n)
           {
            ushort t = StringGetCharacter(s, pos);
            if(t == ',' || t == '}' || t == ' ' || t == '\t' || t == '\r' || t == '\n')
               break;
            pos++;
           }
         value = StringSubstr(s, start, pos - start);
         if(value == "")
            return false;
        }

      int size = ArraySize(keys);
      if(size >= MAX_JSON_FIELDS)
         return false;
      ArrayResize(keys, size + 1);
      ArrayResize(values, size + 1);
      keys[size] = key;
      values[size] = value;

      pos = SkipSpaces(s, pos);
      if(pos >= n)
         return false;
      c = StringGetCharacter(s, pos);
      if(c == '}')
         return true;
      if(c != ',')
         return false;
      pos = SkipSpaces(s, pos + 1);
     }
   return false;
  }

bool JsonGet(const string &keys[], const string &values[], const string key, string &out)
  {
   for(int i = 0; i < ArraySize(keys); i++)
     {
      if(keys[i] == key)
        {
         out = values[i];
         return true;
        }
     }
   return false;
  }

// Strict number check: optional minus, digits, optional fraction and exponent.
bool IsJsonNumber(const string s)
  {
   int n = StringLen(s);
   if(n == 0 || n > 32)
      return false;
   bool digits = false;
   for(int i = 0; i < n; i++)
     {
      ushort c = StringGetCharacter(s, i);
      if(c >= '0' && c <= '9')
         digits = true;
      else if(c != '-' && c != '+' && c != '.' && c != 'e' && c != 'E')
         return false;
     }
   return digits;
  }

bool JsonGetNumber(const string &keys[], const string &values[], const string key, double &out)
  {
   string raw;
   if(!JsonGet(keys, values, key, raw) || !IsJsonNumber(raw))
      return false;
   out = StringToDouble(raw);
   return MathIsValidNumber(out);
  }

// Escapes a string for a JSON value; drops control characters.
string JsonEscape(const string s)
  {
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
     {
      ushort c = StringGetCharacter(s, i);
      if(c == '"' || c == '\\')
         out += "\\" + ShortToString(c);
      else if(c < 32)
         out += " ";
      else
         out += ShortToString(c);
     }
   return out;
  }

//+------------------------------------------------------------------+
//| Signal decoding and validation                                   |
//+------------------------------------------------------------------+
bool IsHexId(const string id)
  {
   if(StringLen(id) != 12)
      return false;
   for(int i = 0; i < 12; i++)
      if(HexDigit(StringGetCharacter(id, i)) < 0)
         return false;
   return true;
  }

bool IsValidSymbolName(const string symbol)
  {
   int n = StringLen(symbol);
   if(n < 1 || n > 24)
      return false;
   for(int i = 0; i < n; i++)
     {
      ushort c = StringGetCharacter(symbol, i);
      bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
                c == '.' || c == '_' || c == '#' || c == '-';
      if(!ok)
         return false;
     }
   return true;
  }

// Decodes the JSON into sig. Returns "" on success or an error message.
string DecodeSignal(const string &keys[], const string &values[], ViewLinkSignal &sig)
  {
   if(!JsonGet(keys, values, "id", sig.id) || !IsHexId(sig.id))
      return "missing or invalid id";
   if(!JsonGet(keys, values, "symbol", sig.symbol) || !IsValidSymbolName(sig.symbol))
      return "invalid symbol";
   if(!JsonGet(keys, values, "side", sig.side) || (sig.side != "buy" && sig.side != "sell"))
      return "side must be buy or sell";
   if(!JsonGet(keys, values, "order", sig.order) ||
      (sig.order != "market" && sig.order != "limit" && sig.order != "stop"))
      return "order must be market, limit or stop";
   if(!JsonGetNumber(keys, values, "lots", sig.lots))
      return "invalid lots";
   if(!JsonGetNumber(keys, values, "entry", sig.entry))
      return "invalid entry";
   if(!JsonGetNumber(keys, values, "sl", sig.sl))
      return "invalid sl";
   if(!JsonGetNumber(keys, values, "tp", sig.tp))
      return "invalid tp";
   if(!JsonGet(keys, values, "comment", sig.comment))
      sig.comment = "ViewLink";
   sig.comment = StringSubstr(sig.comment, 0, 31);
   if(sig.lots <= 0.0 || sig.entry < 0.0 || sig.sl < 0.0 || sig.tp < 0.0)
      return "lots must be > 0 and prices must not be negative";
   return "";
  }

string CheckTradingAllowed(const string symbol)
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      return "algo trading is disabled in the terminal (Algo Trading button)";
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      return "algo trading is not allowed for this EA (check its properties)";
   if(!AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
      return "trading is not allowed on this account";
   if(!SymbolSelect(symbol, true))
      return "unknown symbol " + symbol + " (check the broker suffix in the extension settings)";
   long mode = SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
   if(mode == SYMBOL_TRADE_MODE_DISABLED || mode == SYMBOL_TRADE_MODE_CLOSEONLY)
      return "trading is disabled for " + symbol;
   return "";
  }

int VolumeDigits(const double step)
  {
   int digits = 0;
   double s = step;
   while(digits < 8 && MathAbs(s - MathRound(s)) > 1e-9)
     {
      s *= 10.0;
      digits++;
     }
   return digits;
  }

// Rounds lots down to the symbol volume step. Returns "" or an error message.
string NormalizeLots(const string symbol, double &lots)
  {
   if(lots > InpMaxLots)
      return "lots " + DoubleToString(lots, 2) + " exceed InpMaxLots " + DoubleToString(InpMaxLots, 2);
   double minLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0 || minLot <= 0.0)
      return "cannot read volume settings for " + symbol;
   double normalized = MathFloor(lots / step + 1e-9) * step;
   normalized = NormalizeDouble(normalized, VolumeDigits(step));
   if(normalized < minLot)
      return "lots " + DoubleToString(lots, 4) + " below the symbol minimum " + DoubleToString(minLot, 4);
   if(maxLot > 0.0 && normalized > maxLot)
      return "lots " + DoubleToString(lots, 4) + " above the symbol maximum " + DoubleToString(maxLot, 4);
   lots = normalized;
   return "";
  }

// Rounds a price to the symbol tick size. 0 stays 0 (meaning "not set").
double NormalizePrice(const string symbol, const double price)
  {
   if(price <= 0.0)
      return 0.0;
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double tick = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   double p = price;
   if(tick > 0.0)
      p = MathRound(price / tick) * tick;
   return NormalizeDouble(p, digits);
  }

// Checks entry/SL/TP against the live quote. ref is the expected fill price.
string CheckPrices(const ViewLinkSignal &sig, const MqlTick &tick, const double ref)
  {
   bool   isBuy  = (sig.side == "buy");
   double point  = SymbolInfoDouble(sig.symbol, SYMBOL_POINT);
   double minGap = (double)SymbolInfoInteger(sig.symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;
   int    digits = (int)SymbolInfoInteger(sig.symbol, SYMBOL_DIGITS);
   string refText = DoubleToString(ref, digits);

   if(sig.order == "limit")
     {
      if(isBuy && !(sig.entry < tick.ask - minGap))
         return "buy limit entry must be below ask " + DoubleToString(tick.ask, digits);
      if(!isBuy && !(sig.entry > tick.bid + minGap))
         return "sell limit entry must be above bid " + DoubleToString(tick.bid, digits);
     }
   else if(sig.order == "stop")
     {
      if(isBuy && !(sig.entry > tick.ask + minGap))
         return "buy stop entry must be above ask " + DoubleToString(tick.ask, digits);
      if(!isBuy && !(sig.entry < tick.bid - minGap))
         return "sell stop entry must be below bid " + DoubleToString(tick.bid, digits);
     }

   if(sig.sl > 0.0)
     {
      if(isBuy && !(sig.sl < ref - minGap))
         return "buy SL " + DoubleToString(sig.sl, digits) + " must be below " + refText;
      if(!isBuy && !(sig.sl > ref + minGap))
         return "sell SL " + DoubleToString(sig.sl, digits) + " must be above " + refText;
     }
   if(sig.tp > 0.0)
     {
      if(isBuy && !(sig.tp > ref + minGap))
         return "buy TP " + DoubleToString(sig.tp, digits) + " must be above " + refText;
      if(!isBuy && !(sig.tp < ref - minGap))
         return "sell TP " + DoubleToString(sig.tp, digits) + " must be below " + refText;
     }
   return "";
  }

//+------------------------------------------------------------------+
//| Execution                                                        |
//+------------------------------------------------------------------+
// Fills a rejection report. Always returns false so callers can "return Reject(...)".
bool Reject(ViewLinkReport &report, const string message)
  {
   report.ok = false;
   report.retcode = 0;
   report.ticket = 0;
   report.message = message;
   return false;
  }

// GTC when the symbol supports it, otherwise "good for the day".
ENUM_ORDER_TYPE_TIME PendingTimeType(const string symbol)
  {
   long modes = SymbolInfoInteger(symbol, SYMBOL_EXPIRATION_MODE);
   return ((modes & SYMBOL_EXPIRATION_GTC) != 0) ? ORDER_TIME_GTC : ORDER_TIME_DAY;
  }

bool SendOrder(const ViewLinkSignal &sig)
  {
   bool isBuy = (sig.side == "buy");
   if(sig.order == "market")
      return isBuy ? g_trade.Buy(sig.lots, sig.symbol, 0.0, sig.sl, sig.tp, sig.comment)
                   : g_trade.Sell(sig.lots, sig.symbol, 0.0, sig.sl, sig.tp, sig.comment);
   ENUM_ORDER_TYPE_TIME timeType = PendingTimeType(sig.symbol);
   if(sig.order == "limit")
      return isBuy ? g_trade.BuyLimit(sig.lots, sig.entry, sig.symbol, sig.sl, sig.tp, timeType, 0, sig.comment)
                   : g_trade.SellLimit(sig.lots, sig.entry, sig.symbol, sig.sl, sig.tp, timeType, 0, sig.comment);
   return isBuy ? g_trade.BuyStop(sig.lots, sig.entry, sig.symbol, sig.sl, sig.tp, timeType, 0, sig.comment)
                : g_trade.SellStop(sig.lots, sig.entry, sig.symbol, sig.sl, sig.tp, timeType, 0, sig.comment);
  }

// Validates the signal against the live market and executes it.
// Returns true when the order was accepted; report is filled in every case.
bool ExecuteSignal(ViewLinkSignal &sig, ViewLinkReport &report)
  {
   bool isMarket = (sig.order == "market");
   if(isMarket && !InpAllowMarket)
      return Reject(report, "market orders are disabled in the EA inputs");
   if(!isMarket && !InpAllowPending)
      return Reject(report, "pending orders are disabled in the EA inputs");

   string err = CheckTradingAllowed(sig.symbol);
   if(err != "")
      return Reject(report, err);

   MqlTick tick;
   if(!SymbolInfoTick(sig.symbol, tick) || tick.ask <= 0.0 || tick.bid <= 0.0)
      return Reject(report, "no quotes available for " + sig.symbol + " (market closed?)");

   err = NormalizeLots(sig.symbol, sig.lots);
   if(err != "")
      return Reject(report, err);

   sig.entry = NormalizePrice(sig.symbol, sig.entry);
   sig.sl    = NormalizePrice(sig.symbol, sig.sl);
   sig.tp    = NormalizePrice(sig.symbol, sig.tp);
   if(!isMarket && sig.entry <= 0.0)
      return Reject(report, "entry price is required for " + sig.order + " orders");

   double ref = isMarket ? (sig.side == "buy" ? tick.ask : tick.bid) : sig.entry;
   err = CheckPrices(sig, tick, ref);
   if(err != "")
      return Reject(report, err);

   if(!g_trade.SetTypeFillingBySymbol(sig.symbol))
      LogInfo("could not detect the filling mode of " + sig.symbol + ", using the default");

   bool sent = SendOrder(sig);
   uint retcode = g_trade.ResultRetcode();
   report.retcode = retcode;
   report.ok = sent && (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_PLACED ||
                        retcode == TRADE_RETCODE_DONE_PARTIAL);
   report.ticket = g_trade.ResultOrder();
   if(report.ticket == 0)
      report.ticket = g_trade.ResultDeal();
   report.message = g_trade.ResultRetcodeDescription();
   if(!report.ok)
      report.ticket = 0;
   return report.ok;
  }

//+------------------------------------------------------------------+
//| Bridge round trip                                                |
//+------------------------------------------------------------------+
void PostResult(const string id, const ViewLinkReport &report)
  {
   string body = "{\"id\":\"" + JsonEscape(id) + "\"" +
                 ",\"ok\":" + (report.ok ? "true" : "false") +
                 ",\"retcode\":" + IntegerToString((long)report.retcode) +
                 ",\"ticket\":" + IntegerToString((long)report.ticket) +
                 ",\"message\":\"" + JsonEscape(StringSubstr(report.message, 0, 180)) + "\"}";
   string response;
   int status = HttpRequest("POST", "/result", body, response);
   if(status != 200)
      LogErrorOnce("could not report result for signal " + id + " (HTTP " + IntegerToString(status) +
                   "): " + response);
  }

void ProcessResponse(const string json)
  {
   string keys[], values[];
   if(!ParseFlatJson(json, keys, values))
     {
      LogErrorOnce("bridge returned invalid JSON: " + StringSubstr(json, 0, 120));
      return;
     }
   if(ArraySize(keys) == 0)
      return;   // empty queue

   ViewLinkSignal sig;
   ViewLinkReport report;
   Reject(report, "");
   string err = DecodeSignal(keys, values, sig);
   if(err != "")
     {
      Reject(report, "invalid signal: " + err);
      LogInfo("signal rejected: " + report.message);
      if(IsHexId(sig.id))
         PostResult(sig.id, report);
      return;
     }

   LogInfo("signal " + sig.id + " received: " + sig.side + " " + sig.order + " " +
           DoubleToString(sig.lots, 2) + " " + sig.symbol);
   if(ExecuteSignal(sig, report))
      LogInfo("signal " + sig.id + " executed, ticket " + IntegerToString((long)report.ticket) +
              " (" + report.message + ")");
   else
      LogInfo("signal " + sig.id + " rejected: " + report.message +
              (report.retcode > 0 ? " (retcode " + IntegerToString((long)report.retcode) + ")" : ""));
   PostResult(sig.id, report);
  }

void PollBridge()
  {
   string response;
   int status = HttpRequest("GET", "/next", "", response);
   if(status == -1 || status == 401)
      return;   // already logged
   if(status != 200)
     {
      LogErrorOnce("unexpected HTTP " + IntegerToString(status) + " from /next: " + StringSubstr(response, 0, 120));
      return;
     }
   ClearLastError();
   ProcessResponse(response);
  }

//+------------------------------------------------------------------+
//| Event handlers                                                   |
//+------------------------------------------------------------------+
int OnInit()
  {
   if(MQLInfoInteger(MQL_TESTER))
     {
      Print("ViewLink ERROR: WebRequest is not available in the Strategy Tester.");
      return INIT_FAILED;
     }
   g_baseUrl = InpBridgeUrl;
   StringTrimLeft(g_baseUrl);
   StringTrimRight(g_baseUrl);
   while(StringLen(g_baseUrl) > 0 && StringGetCharacter(g_baseUrl, StringLen(g_baseUrl) - 1) == '/')
      g_baseUrl = StringSubstr(g_baseUrl, 0, StringLen(g_baseUrl) - 1);

   if(StringFind(g_baseUrl, "http://127.0.0.1") != 0 && StringFind(g_baseUrl, "http://localhost") != 0)
     {
      Print("ViewLink ERROR: InpBridgeUrl must point to the local bridge (http://127.0.0.1:<port>).");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(StringLen(InpApiKey) == 0)
     {
      Print("ViewLink ERROR: InpApiKey is empty. Copy the key printed by bridge/server.py.");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpMaxLots <= 0.0)
     {
      Print("ViewLink ERROR: InpMaxLots must be > 0.");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpPollMs < 250)
     {
      Print("ViewLink ERROR: InpPollMs must be >= 250 ms.");
      return INIT_PARAMETERS_INCORRECT;
     }

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints((ulong)InpDeviation);
   g_trade.SetAsyncMode(false);

   if(!EventSetMillisecondTimer(InpPollMs))
     {
      Print("ViewLink ERROR: cannot create the timer (error ", GetLastError(), ").");
      return INIT_FAILED;
     }
   LogInfo("started, polling " + g_baseUrl + " every " + IntegerToString(InpPollMs) +
           " ms, max lots " + DoubleToString(InpMaxLots, 2));
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      LogInfo("warning: Algo Trading is disabled in the terminal, signals will be rejected.");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   LogInfo("stopped (reason " + IntegerToString(reason) + ")");
  }

void OnTimer()
  {
   PollBridge();
  }

void OnTick()
  {
   // Work is driven by the timer; nothing to do on ticks.
  }
//+------------------------------------------------------------------+
