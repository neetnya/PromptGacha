using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace PromptGacha
{
    /// <summary>一次抽卡的全部参数，和 anima_cli.py 的命令行一一对应。</summary>
    public sealed class AnimaOptions
    {
        // girl
        public string Pool = "热门 TOP200";
        public int Count = 1;
        public string Relation = "auto";
        public bool HugeBreasts;
        public bool NoAvoidRepeat;

        // roll
        public bool NoOutfit;
        public bool NoPose;
        public bool NoExpression;
        public bool NoScene;
        public bool AllowPublic;

        // 通用
        public string Level = "suggestive";
        public int Repeat = 1;

        /// <summary>拼出子命令之后的参数列表（不含 python / 脚本路径）。</summary>
        public List<string> ToArgs(string cmd, string claimIn, string claimOut)
        {
            var a = new List<string> { cmd };
            a.Add("--level=" + (string.IsNullOrWhiteSpace(Level) ? "suggestive" : Level));
            a.Add("--json");
            a.Add("--repeat=" + Math.Max(1, Repeat));

            if (cmd == "girl")
            {
                a.Add("--pool=" + (string.IsNullOrWhiteSpace(Pool) ? "热门 TOP200" : Pool));
                a.Add("--count=" + (Count < 1 ? 1 : (Count > 3 ? 3 : Count)));
                a.Add("--relation=" + (string.IsNullOrWhiteSpace(Relation) ? "auto" : Relation));
                if (HugeBreasts) a.Add("--huge-breasts");
                if (NoAvoidRepeat) a.Add("--no-avoid-repeat");
            }
            else
            {
                if (NoOutfit) a.Add("--no-outfit");
                if (NoPose) a.Add("--no-pose");
                if (NoExpression) a.Add("--no-expression");
                if (NoScene) a.Add("--no-scene");
                if (AllowPublic) a.Add("--allow-public");
            }

            if (!string.IsNullOrEmpty(claimIn))
                a.Add("--claim-in=" + claimIn);
            if (!string.IsNullOrEmpty(claimOut))
                a.Add("--claim-out=" + claimOut);
            return a;
        }
    }

    public sealed class ResultItem
    {
        public bool Ok;
        public string Text = "";
        public string Report = "";
        public string ClaimJson = "";
    }

    public sealed class RunOutcome
    {
        public int ExitCode;
        public string StdErr = "";
        public string StdOut = "";
        public List<ResultItem> Items = new List<ResultItem>();
        public string Failure;          // 非空 = 这次调用彻底失败（找不到 python / 解析不出 JSON 等）

        public bool Ok { get { return Failure == null && Items.Count > 0; } }

        public string AllText
        {
            get
            {
                var sb = new StringBuilder();
                foreach (var it in Items)
                {
                    if (sb.Length > 0) sb.AppendLine();
                    sb.Append(it.Text);
                }
                return sb.ToString();
            }
        }

        public string AllReport
        {
            get
            {
                var sb = new StringBuilder();
                foreach (var it in Items)
                {
                    if (sb.Length > 0) sb.AppendLine().AppendLine(new string('=', 60)).AppendLine();
                    sb.AppendLine(LogText.Clean(it.Report));
                }
                if (!string.IsNullOrEmpty(StdErr)) sb.AppendLine().AppendLine(StdErr.TrimEnd());
                return sb.ToString().TrimEnd();
            }
        }
    }

    /// <summary>整理引擎日志：去掉 seed=NNN（界面不暴露种子）和 emoji（文本框里显示成豆腐块）。</summary>
    internal static class LogText
    {
        private static readonly Regex SeedRe = new Regex(@"[ \t]*seed=\d+[ \t]*", RegexOptions.Compiled);
        private static readonly Regex EmojiRe = new Regex(@"[\uD83C-\uDBFF][\uDC00-\uDFFF]", RegexOptions.Compiled);
        private static readonly Regex SpacesRe = new Regex(@" {2,}", RegexOptions.Compiled);

        public static string Clean(string log)
        {
            if (string.IsNullOrEmpty(log)) return log;

            var lines = log.Replace("\r\n", "\n").Split('\n');
            for (int i = 0; i < lines.Length; i++)
            {
                string s = lines[i];
                if (s.IndexOf("seed=", StringComparison.Ordinal) >= 0)
                {
                    s = SeedRe.Replace(s, " ");
                    s = SpacesRe.Replace(s, " ");
                }
                string noEmoji = EmojiRe.Replace(s, "");
                if (noEmoji.Length != s.Length && noEmoji.StartsWith(" ")) noEmoji = noEmoji.TrimStart();
                lines[i] = noEmoji.TrimEnd();
            }
            return string.Join("\n", lines);
        }
    }

    /// <summary>调用 anima_cli.py 的薄封装：只管起进程、收输出、解 JSON。</summary>
    public static class CliRunner
    {
        public static async Task<RunOutcome> RunAsync(
            string pythonExe, IList<string> pythonPrefix, string engineDir,
            IList<string> args, CancellationToken ct)
        {
            var outcome = new RunOutcome();
            string script = Path.Combine(engineDir, "anima_cli.py");
            if (!File.Exists(script))
            {
                outcome.Failure = "找不到引擎脚本：" + script;
                return outcome;
            }

            var psi = new ProcessStartInfo
            {
                FileName = pythonExe,
                WorkingDirectory = engineDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            if (pythonPrefix != null)
                foreach (var x in pythonPrefix) psi.ArgumentList.Add(x);
            psi.ArgumentList.Add("-X");
            psi.ArgumentList.Add("utf8");
            psi.ArgumentList.Add(script);
            foreach (var x in args) psi.ArgumentList.Add(x);
            psi.Environment["PYTHONIOENCODING"] = "utf-8";
            psi.Environment["PYTHONUTF8"] = "1";

            var stdout = new StringBuilder();
            var stderr = new StringBuilder();
            try
            {
                using (var p = new Process { StartInfo = psi })
                {
                    p.OutputDataReceived += (s, e) => { if (e.Data != null) stdout.AppendLine(e.Data); };
                    p.ErrorDataReceived += (s, e) => { if (e.Data != null) stderr.AppendLine(e.Data); };
                    p.Start();
                    p.BeginOutputReadLine();
                    p.BeginErrorReadLine();

                    try
                    {
                        await p.WaitForExitAsync(ct).ConfigureAwait(false);
                    }
                    catch (OperationCanceledException)
                    {
                        try { p.Kill(true); } catch { }
                        outcome.Failure = "已取消";
                        return outcome;
                    }
                    outcome.ExitCode = p.ExitCode;
                }
            }
            catch (OperationCanceledException)
            {
                outcome.Failure = "已取消";
                return outcome;
            }
            catch (Exception ex)
            {
                outcome.Failure = "启动 Python 失败：" + ex.Message +
                                  "\n检查环境变量 PATH 里是否有可用的 python.exe（当前：" + pythonExe + "）";
                return outcome;
            }

            outcome.StdOut = stdout.ToString();
            outcome.StdErr = stderr.ToString();

            var items = Parse(outcome.StdOut, out string parseError);
            outcome.Items = items;
            if (items.Count == 0)
            {
                string tail = outcome.StdErr.Trim();
                outcome.Failure = string.IsNullOrEmpty(tail)
                    ? "没有拿到结果（退出码 " + outcome.ExitCode + "）" +
                      (string.IsNullOrEmpty(parseError) ? "" : "\n" + parseError)
                    : tail;
            }
            return outcome;
        }

        /// <summary>--json 单个结果是对象，-r>1 时是数组。</summary>
        public static List<ResultItem> Parse(string json, out string error)
        {
            error = null;
            var list = new List<ResultItem>();
            if (string.IsNullOrWhiteSpace(json)) return list;
            try
            {
                using (var doc = JsonDocument.Parse(json))
                {
                    if (doc.RootElement.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var el in doc.RootElement.EnumerateArray())
                            list.Add(ReadItem(el));
                    }
                    else if (doc.RootElement.ValueKind == JsonValueKind.Object)
                    {
                        list.Add(ReadItem(doc.RootElement));
                    }
                    else
                    {
                        error = "JSON 根节点不是对象/数组";
                    }
                }
            }
            catch (JsonException ex)
            {
                error = "JSON 解析失败：" + ex.Message;
                list.Clear();
            }
            return list;
        }

        private static ResultItem ReadItem(JsonElement el)
        {
            var it = new ResultItem();
            if (el.TryGetProperty("ok", out var ok)) it.Ok = ok.ValueKind == JsonValueKind.True;
            if (el.TryGetProperty("text", out var text) && text.ValueKind == JsonValueKind.String)
                it.Text = text.GetString() ?? "";
            if (el.TryGetProperty("report", out var rep) && rep.ValueKind == JsonValueKind.String)
                it.Report = rep.GetString() ?? "";
            if (el.TryGetProperty("claim", out var claim))
                it.ClaimJson = claim.GetRawText();
            return it;
        }
    }
}
