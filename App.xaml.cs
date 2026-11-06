using System;
using System.IO;
using System.Windows;
using System.Windows.Threading;

namespace PromptGacha
{
    public partial class App : Application
    {
        private static readonly string LogDir =
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "PromptGacha");

        protected override void OnStartup(StartupEventArgs e)
        {
            base.OnStartup(e);
            DispatcherUnhandledException += OnDispatcherUnhandled;
            AppDomain.CurrentDomain.UnhandledException += OnDomainUnhandled;
        }

        private void OnDispatcherUnhandled(object sender, DispatcherUnhandledExceptionEventArgs e)
        {
            WriteError(e.Exception);
            MessageBox.Show("程序出错：\n" + e.Exception.Message +
                            "\n\n日志：" + Path.Combine(LogDir, "error.log"),
                            "PromptGacha", MessageBoxButton.OK, MessageBoxImage.Error);
            e.Handled = true;
        }

        private static void OnDomainUnhandled(object sender, UnhandledExceptionEventArgs e)
        {
            WriteError(e.ExceptionObject as Exception);
        }

        private static void WriteError(Exception ex)
        {
            if (ex == null) return;
            try
            {
                Directory.CreateDirectory(LogDir);
                File.AppendAllText(Path.Combine(LogDir, "error.log"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + ex + Environment.NewLine + Environment.NewLine);
            }
            catch
            {
                // 日志写不进去就算了，不要因为日志再抛一次
            }
        }
    }
}
