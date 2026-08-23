# frozen_string_literal: true

gem_root = File.expand_path('ox-gem', __dir__)
Gem.use_paths(gem_root, [gem_root])
require 'ox'

poc_path = ARGV.find { |arg| File.file?(arg) }
exit 0 unless poc_path

data = File.binread(poc_path)
exit 0 if data.bytesize < 100

begin
  Ox.parse(data)
rescue Ox::ParseError, Ox::SyntaxError, EncodingError
end
