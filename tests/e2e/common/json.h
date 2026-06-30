// Minimal JSON reader for the machine-generated manifest. Recursive descent over
// the controlled subset the harness emits: objects, arrays, strings (basic
// escapes), integers/doubles, true/false/null. Not a general-purpose parser.
#pragma once
#include <string>
#include <vector>
#include <map>
#include <cstdlib>
#include <cstdio>
#include <stdexcept>

namespace js {

struct Value {
    enum Type { Null, Bool, Num, Str, Arr, Obj } type = Null;
    bool b = false;
    double num = 0.0;
    std::string str;
    std::vector<Value> arr;
    std::map<std::string, Value> obj;

    bool isNull() const { return type == Null; }
    bool has(const std::string& k) const {
        return type == Obj && obj.find(k) != obj.end();
    }
    const Value& operator[](const std::string& k) const {
        static const Value nil;
        auto it = obj.find(k);
        return it == obj.end() ? nil : it->second;
    }
    const Value& operator[](size_t i) const {
        static const Value nil;
        return i < arr.size() ? arr[i] : nil;
    }
    size_t size() const { return type == Arr ? arr.size() : obj.size(); }
    int asInt() const { return (int)num; }
    bool asBool() const { return b; }
    const std::string& asStr() const { return str; }
};

class Parser {
public:
    explicit Parser(const std::string& s) : s_(s), i_(0) {}
    Value parse() {
        Value v = value();
        ws();
        return v;
    }

private:
    const std::string& s_;
    size_t i_;

    void ws() {
        while (i_ < s_.size()) {
            char c = s_[i_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') i_++;
            else break;
        }
    }
    char peek() { return i_ < s_.size() ? s_[i_] : '\0'; }
    void expect(char c) {
        if (peek() != c) throw std::runtime_error(std::string("json: expected ") + c);
        i_++;
    }

    Value value() {
        ws();
        char c = peek();
        if (c == '{') return object();
        if (c == '[') return array();
        if (c == '"') { Value v; v.type = Value::Str; v.str = str(); return v; }
        if (c == 't' || c == 'f') return boolean();
        if (c == 'n') { i_ += 4; return Value(); }  // null
        return number();
    }

    std::string str() {
        expect('"');
        std::string out;
        while (i_ < s_.size()) {
            char c = s_[i_++];
            if (c == '"') break;
            if (c == '\\') {
                char e = s_[i_++];
                switch (e) {
                    case 'n': out += '\n'; break;
                    case 't': out += '\t'; break;
                    case 'r': out += '\r'; break;
                    case '"': out += '"'; break;
                    case '\\': out += '\\'; break;
                    case '/': out += '/'; break;
                    default: out += e; break;
                }
            } else {
                out += c;
            }
        }
        return out;
    }

    Value boolean() {
        Value v;
        v.type = Value::Bool;
        if (s_.compare(i_, 4, "true") == 0) { v.b = true; i_ += 4; }
        else { v.b = false; i_ += 5; }
        return v;
    }

    Value number() {
        size_t start = i_;
        while (i_ < s_.size()) {
            char c = s_[i_];
            if ((c >= '0' && c <= '9') || c == '-' || c == '+' ||
                c == '.' || c == 'e' || c == 'E') i_++;
            else break;
        }
        Value v;
        v.type = Value::Num;
        v.num = std::atof(s_.substr(start, i_ - start).c_str());
        return v;
    }

    Value array() {
        Value v;
        v.type = Value::Arr;
        expect('[');
        ws();
        if (peek() == ']') { i_++; return v; }
        while (true) {
            v.arr.push_back(value());
            ws();
            if (peek() == ',') { i_++; continue; }
            break;
        }
        expect(']');
        return v;
    }

    Value object() {
        Value v;
        v.type = Value::Obj;
        expect('{');
        ws();
        if (peek() == '}') { i_++; return v; }
        while (true) {
            ws();
            std::string k = str();
            ws();
            expect(':');
            v.obj[k] = value();
            ws();
            if (peek() == ',') { i_++; continue; }
            break;
        }
        expect('}');
        return v;
    }
};

inline Value parse(const std::string& text) { return Parser(text).parse(); }

inline Value parse_file(const std::string& path) {
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) throw std::runtime_error("json: cannot open " + path);
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    std::string buf(n, '\0');
    if (n > 0) { size_t rd = fread(&buf[0], 1, n, f); buf.resize(rd); }
    fclose(f);
    return parse(buf);
}

}  // namespace js
