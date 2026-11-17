using System;

namespace Contract
{
    public interface IGreeter
    {
        string Greet(string name);
    }

    public class BaseGreeter
    {
        public string Prefix() { return "Hello"; }
    }

    public class PoliteGreeter : BaseGreeter, IGreeter
    {
        public string Greet(string name)
        {
            return Prefix() + ", " + Helper.Trim(name);
        }
    }

    public static class Helper
    {
        public static string Trim(string value) { return value.Trim(); }
    }
}
