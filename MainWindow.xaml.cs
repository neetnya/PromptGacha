using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;

namespace PromptGacha
{
    public partial class MainWindow : Window
    {
        private CancellationTokenSource _cts;

        // Python 一律从环境变量 PATH 里找；引擎固定用软件目录（dist/engine）
        private string _pythonExe = "python";
        private List<string> _pythonPrefix = new List<string>();
        private string _engineDir = "";
        private bool _pythonFound;

        public MainWindow()
        {
            InitializeComponent();
            ResolvePython();
            _engineDir = ResolveEngineDir();

            if (!File.Exists(Path.Combine(_engineDir, "anima_cli.py")))
                AppendLog("找不到引擎：未在软件目录下发现 anima_cli.py（" + _engineDir + "）");
            if (!_pythonFound)
                AppendLog("找不到 Python：环境变量 PATH 里没有 python.exe，抽卡会失败。");
        }

        // ==================== 启动期解析 ====================

        private void ResolvePython()
        {
            // 1) PATH 里的 python.exe（跳过 Microsoft Store 的 WindowsApps 占位）
            try
            {
                string pathVar = Environment.GetEnvironmentVariable("PATH") ?? "";
                foreach (string raw in pathVar.Split(';'))
                {
                    string dir = raw.Trim().Trim('"');
                    if (dir.Length == 0) continue;
                    if (dir.IndexOf("WindowsApps", StringComparison.OrdinalIgnoreCase) >= 0) continue;
                    string cand = Path.Combine(dir, "python.exe");
                    if (File.Exists(cand))
                    {
                        _pythonExe = cand;
                        _pythonPrefix.Clear();
                        _pythonFound = true;
                        return;
                    }
                }
            }
            catch { }

            // 2) PATH 里没有，退回系统自带的 py 启动器
            string py = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "py.exe");
            if (File.Exists(py))
            {
                _pythonExe = py;
                _pythonPrefix = new List<string> { "-3" };
                _pythonFound = true;
                return;
            }

            _pythonExe = "python";
            _pythonFound = false;
        }

        private static string ResolveEngineDir()
        {
            try
            {
                string baseDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
                string sub = Path.Combine(baseDir, "engine");
                if (File.Exists(Path.Combine(sub, "anima_cli.py"))) return sub;
                if (File.Exists(Path.Combine(baseDir, "anima_cli.py"))) return baseDir;

                // 开发时 exe 在 gui/dist 下，往上找仓库根目录
                string dir = baseDir;
                for (int i = 0; i < 5 && !string.IsNullOrEmpty(dir); i++)
                {
                    if (File.Exists(Path.Combine(dir, "anima_cli.py"))) return dir;
                    dir = Path.GetDirectoryName(dir);
                }
                return sub;
            }
            catch
            {
                return AppDomain.CurrentDomain.BaseDirectory;
            }
        }

        // ==================== 抽卡 ====================

        private AnimaOptions CollectOptions()
        {
            return new AnimaOptions
            {
                Level = CmbText(CmbLevel),
                Repeat = IntOf(TxtRepeat, 1, 1, 99),
                Pool = CmbText(CmbPool),
                Count = IntOf(CmbText(CmbCount), 1, 1, 3),
                Relation = CmbText(CmbRelation),
                HugeBreasts = ChkHuge.IsChecked == true,
                NoAvoidRepeat = ChkNoAvoid.IsChecked == true,
                NoOutfit = ChkNoOutfit.IsChecked == true,
                NoPose = ChkNoPose.IsChecked == true,
                NoExpression = ChkNoExpr.IsChecked == true,
                NoScene = ChkNoScene.IsChecked == true,
                AllowPublic = ChkAllowPublic.IsChecked == true,
            };
        }

        private async void OnGirl(object sender, RoutedEventArgs e)
        {
            var o = CollectOptions();
            await DrawAsync(async ct =>
            {
                var r = await ExecAsync("girl", o, null, ct);
                return r;
            });
        }

        private async void OnRoll(object sender, RoutedEventArgs e)
        {
            var o = CollectOptions();
            await DrawAsync(async ct =>
            {
                var r = await ExecAsync("roll", o, null, ct);
                return r;
            });
        }

        /// <summary>girl + roll：先抽特征拿到声明，再把声明喂给 roll 追加服装等。</summary>
        private async void OnGirlRoll(object sender, RoutedEventArgs e)
        {
            var o = CollectOptions();
            await DrawAsync(async ct =>
            {
                var g = await ExecAsync("girl", o, null, ct);
                if (g.Failure != null || g.Items.Count == 0) return g;   // 失败原样返回
                var last = g.Items[g.Items.Count - 1];
                if (!last.Ok || string.IsNullOrEmpty(last.ClaimJson))
                {
                    AppendLog("girl 没有产出可接力的状态，已停止。");
                    return g;
                }
                return await ExecAsync("roll", o, last.ClaimJson, ct);
            });
        }

        /// <summary>清空日志 → 跑一次抽卡流程 → 把结果摊到界面上。</summary>
        private async Task DrawAsync(Func<CancellationToken, Task<RunOutcome>> body)
        {
            if (!File.Exists(Path.Combine(_engineDir, "anima_cli.py")))
            {
                MessageBox.Show(this, "软件目录下没有 anima_cli.py，引擎不完整。\n期望位置：" +
                    Path.Combine(_engineDir, "anima_cli.py"),
                    "PromptGacha", MessageBoxButton.OK, MessageBoxImage.Warning);
                return;
            }

            TxtLog.Text = "";

            var cts = new CancellationTokenSource();
            _cts = cts;
            SetBusy(true);
            var sw = Stopwatch.StartNew();

            RunOutcome outcome;
            try
            {
                outcome = await body(cts.Token);
            }
            catch (Exception ex)
            {
                outcome = new RunOutcome { Failure = ex.Message };
            }
            finally
            {
                sw.Stop();
                SetBusy(false);
                _cts = null;
            }

            if (outcome.Failure != null)
            {
                AppendLog(outcome.Failure);
                TxtResult.Text = "";
                TxtMeta.Text = "";
                TxtStatus.Text = "失败：" + FirstLine(outcome.Failure);
                return;
            }
            if (outcome.Items.Count == 0)
            {
                TxtResult.Text = "";
                TxtMeta.Text = "";
                TxtStatus.Text = "没有拿到结果";
                return;
            }

            var last = outcome.Items[outcome.Items.Count - 1];
            if (!last.Ok)
            {
                TxtResult.Text = "";
                TxtMeta.Text = "";
                TxtStatus.Text = "抽取失败：" + FirstLine(last.Report);
                return;
            }

            TxtResult.Text = outcome.AllText;
            TxtMeta.Text = outcome.Items.Count + " 条";
            TxtStatus.Text = "完成 · " + sw.ElapsedMilliseconds + " ms";
        }

        /// <summary>调一次引擎，日志头 + 报告直接落到日志框。</summary>
        private async Task<RunOutcome> ExecAsync(string cmd, AnimaOptions o,
                                                 string claimInJson, CancellationToken ct)
        {
            string runDir = Path.Combine(Path.GetTempPath(), "PromptGacha");
            Directory.CreateDirectory(runDir);
            string claimOut = Path.Combine(runDir, "claim_" + cmd + ".json");
            string claimIn = Path.Combine(runDir, "claim_in_" + cmd + ".json");

            if (File.Exists(claimOut)) File.Delete(claimOut);
            if (File.Exists(claimIn)) File.Delete(claimIn);

            string claimInPath = null;
            if (!string.IsNullOrEmpty(claimInJson))
            {
                File.WriteAllText(claimIn, claimInJson, new UTF8Encoding(false));
                claimInPath = claimIn;
            }

            var args = o.ToArgs(cmd, claimInPath, claimOut);
            AppendLog("—— " + cmd + (claimInPath != null ? "（接上一步的结果）" : "") +
                      "  level=" + o.Level + "  repeat=" + o.Repeat + " ——");

            var outcome = await CliRunner.RunAsync(_pythonExe, _pythonPrefix, _engineDir, args, ct);
            AppendLog(outcome.Failure ?? outcome.AllReport);
            return outcome;
        }

        private void OnCancel(object sender, RoutedEventArgs e)
        {
            if (_cts != null)
            {
                try { _cts.Cancel(); } catch { }
                AppendLog("（已请求取消）");
            }
        }

        // ==================== 输出区 ====================

        private void OnCopy(object sender, RoutedEventArgs e)
        {
            string text = TxtResult.Text ?? "";
            if (text.Length == 0) return;
            try
            {
                Clipboard.SetText(text);
                TxtStatus.Text = "已复制 " + text.Length + " 字符";
            }
            catch (Exception ex)
            {
                TxtStatus.Text = "复制失败：" + ex.Message;
            }
        }

        private void OnSave(object sender, RoutedEventArgs e)
        {
            string text = TxtResult.Text ?? "";
            if (text.Length == 0) return;
            var dlg = new SaveFileDialog
            {
                Title = "保存提示词",
                Filter = "文本文件|*.txt|所有文件|*.*",
                FileName = "anima_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt",
            };
            if (dlg.ShowDialog(this) == true)
            {
                try
                {
                    File.WriteAllText(dlg.FileName, text, new UTF8Encoding(false));
                    TxtStatus.Text = "已保存：" + dlg.FileName;
                }
                catch (Exception ex)
                {
                    MessageBox.Show(this, "保存失败：" + ex.Message, "PromptGacha",
                        MessageBoxButton.OK, MessageBoxImage.Error);
                }
            }
        }

        private void AppendLog(string text)
        {
            if (string.IsNullOrEmpty(text)) return;
            if (TxtLog.Text.Length > 0) TxtLog.AppendText(Environment.NewLine);
            TxtLog.AppendText(text);
            TxtLog.ScrollToEnd();
        }

        private void SetBusy(bool busy)
        {
            BtnGirl.IsEnabled = !busy;
            BtnGirlRoll.IsEnabled = !busy;
            BtnRoll.IsEnabled = !busy;
            BtnCancel.IsEnabled = busy;
            TxtStatus.Text = busy ? "抽卡中…" : TxtStatus.Text;
        }

        // ==================== 小工具 ====================

        private static string CmbText(ComboBox cmb)
        {
            var item = cmb.SelectedItem as ComboBoxItem;
            if (item != null)
            {
                string tag = item.Tag as string;      // 显示名和取值不同时用 Tag
                if (!string.IsNullOrEmpty(tag)) return tag;
                return Convert.ToString(item.Content) ?? "";
            }
            return Convert.ToString(cmb.SelectedItem) ?? "";
        }

        private static int IntOf(TextBox box, int fallback, int min, int max)
        {
            int v;
            if (!int.TryParse((box.Text ?? "").Trim(), out v)) return fallback;
            if (v < min) v = min;
            if (v > max) v = max;
            return v;
        }

        private static int IntOf(string text, int fallback, int min, int max)
        {
            int v;
            if (!int.TryParse((text ?? "").Trim(), out v)) return fallback;
            if (v < min) v = min;
            if (v > max) v = max;
            return v;
        }

        private static string FirstLine(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            int i = s.IndexOf('\n');
            return i < 0 ? s : s.Substring(0, i).TrimEnd();
        }
    }
}
